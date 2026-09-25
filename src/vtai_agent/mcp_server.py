"""MCP server surface.

One registration line in opencode / Claude Code / pi exposes every tool here as
a native tool of that agent. Errors from guardrails are returned as tool results
(never crash the session); allowed/denied decisions still hit the audit log.
"""

from mcp.server.mcpserver import MCPServer

from . import __version__, db
from .config import get_settings
from .guardrails import GuardrailViolation, reset_current_run, set_current_run
from .tools import (
    BackupSyncTool,
    DelegateCodingTool,
    DiskAuditTool,
    MediaTranscodeTool,
    OrganizeFilesTool,
    ShellTool,
    SystemInfoTool,
    ToolError,
    ToolResult,
    registry,
)


def register_tools() -> None:
    if registry.all():  # idempotent: safe to call from CLI, MCP, and tests
        return
    registry.register(SystemInfoTool())
    registry.register(ShellTool())
    registry.register(OrganizeFilesTool())
    registry.register(BackupSyncTool())
    registry.register(MediaTranscodeTool())
    registry.register(DiskAuditTool())
    registry.register(DelegateCodingTool())


def build_server() -> MCPServer:
    register_tools()
    server = MCPServer(
        name="vtai-agent",
        description=(
            "VT-AIAgent: system automation for this machine — file organization, "
            "allowlisted shell, system info. Guarded: sandboxed paths, dry-run by "
            "default, fully audited."
        ),
        instructions=(
            "Prefer dry_run=true to preview before acting. File moves stay inside "
            "the target directory (writable_roots). Shell binaries not in the "
            "allowlist are rejected. All actions are logged to the audit DB."
        ),
        version=__version__,
    )

    @server.tool()
    async def system_info(include_gpu: bool = True) -> str:
        """Snapshot this machine: CPU, memory, disks, load, and AMD GPU state.
        Read-only and safe to call before deciding how to act.

        Args:
            include_gpu: Probe rocm-smi/vulkaninfo for GPU state (default true).
        """
        return await _call("system_info", {"include_gpu": include_gpu})

    @server.tool()
    async def run_shell(command: str, dry_run: bool | None = None,
                        timeout: float = 30.0) -> str:
        """Run an allowlisted shell command. Protected paths and non-allowlisted
        binaries are rejected. Use dry_run=true to preview without executing.

        Args:
            command: Shell command; argv[0] must be in the allowlist.
            dry_run: Preview only (defaults to config dry_run_default).
            timeout: Seconds before the command is killed (1-300).
        """
        return await _call("run_shell",
                           {"command": command, "dry_run": dry_run, "timeout": timeout})

    @server.tool()
    async def organize_files(directory: str = "~/Downloads",
                             dry_run: bool | None = None,
                             recursive: bool = False,
                             confirm_mime: bool = True) -> str:
        """Content-aware cleanup: sort a directory's files into Documents/Images/
        Videos/Audio/Archives/Installers/Code/Data/Fonts/Models. Collisions are
        auto-renamed. Everything stays inside the target directory.

        Args:
            directory: Directory to tidy (default ~/Downloads).
            dry_run: Plan only, move nothing (defaults to config dry_run_default).
            recursive: Descend into subdirectories.
            confirm_mime: Use `file` mime type for unknown extensions.
        """
        return await _call("organize_files", {
            "directory": directory, "dry_run": dry_run,
            "recursive": recursive, "confirm_mime": confirm_mime,
        })

    @server.tool()
    async def backup_sync(source: str, destination: str,
                          dry_run: bool | None = None,
                          delete_extraneous: bool = False,
                          compress: bool = True,
                          verify: bool = True) -> str:
        """Incremental directory backup via rsync. Reports the transfer plan and
        can verify with a checksum pass. Deletion of extraneous files is opt-in
        and never happens during a dry run.

        Args:
            source: Directory to back up.
            destination: Backup destination (must be in writable_roots).
            dry_run: Preview only (defaults to config dry_run_default).
            delete_extraneous: Delete destination files not in source (dangerous).
            compress: Use rsync -z.
            verify: Re-run with checksums after transfer.
        """
        return await _call("backup_sync", {
            "source": source, "destination": destination, "dry_run": dry_run,
            "delete_extraneous": delete_extraneous, "compress": compress,
            "verify": verify,
        })

    @server.tool()
    async def transcode_media(directory: str,
                              dry_run: bool | None = None,
                              codec: str | None = None,
                              crf: int | None = None,
                              recursive: bool = True,
                              overwrite: bool = False) -> str:
        """Batch transcode videos with AMD GPU (VAAPI) acceleration. Output goes
        to a 'transcoded' subfolder; originals are never modified.

        Args:
            directory: Directory containing videos.
            dry_run: Report jobs without encoding.
            codec: h264_vaapi | hevc_vaapi | av1_vaapi | libx264 (config default).
            crf: Quality for software codecs, 0-51 (lower = better).
            recursive: Descend into subdirectories.
            overwrite: Re-encode even if output exists.
        """
        return await _call("transcode_media", {
            "directory": directory, "dry_run": dry_run, "codec": codec, "crf": crf,
            "recursive": recursive, "overwrite": overwrite,
        })

    @server.tool()
    async def disk_audit(path: str = "/",
                         min_size_mb: int = 100,
                         older_than_days: int = 30,
                         limit: int = 25,
                         include_caches: bool = True) -> str:
        """Read-only scan for reclaimable disk space: largest/oldest files plus
        cache directory sizes. Deletes nothing.

        Args:
            path: Root to scan.
            min_size_mb: Only report files at least this large.
            older_than_days: Only report files older than this.
            limit: Max files to report.
            include_caches: Also measure known cache directories.
        """
        return await _call("disk_audit", {
            "path": path, "min_size_mb": min_size_mb,
            "older_than_days": older_than_days, "limit": limit,
            "include_caches": include_caches,
        })

    @server.tool()
    async def delegate_coding(cli: str, prompt: str, working_dir: str,
                              dry_run: bool | None = None,
                              verify_command: str | None = None,
                              auto_rollback: bool = True,
                              timeout: float = 600.0,
                              skip_permissions: bool = False) -> str:
        """Delegate a coding task to opencode, claude, or pi in headless mode.
        Snapshots the repo, captures the diff, and rolls back if an optional
        verify command fails. The working directory must be a git repo.

        Args:
            cli: opencode | claude | pi
            prompt: The coding task to perform.
            working_dir: Project directory (must be a git repo, in writable_roots).
            dry_run: Snapshot and plan only (defaults to config dry_run_default).
            verify_command: Allowlisted command that must pass afterward, e.g. "pytest".
            auto_rollback: Restore the snapshot if verification fails.
            timeout: Seconds before the delegate is killed.
            skip_permissions: Allow --dangerously-skip-permissions (unattended runs).
        """
        return await _call("delegate_coding", {
            "cli": cli, "prompt": prompt, "working_dir": working_dir,
            "dry_run": dry_run, "verify_command": verify_command,
            "auto_rollback": auto_rollback, "timeout": timeout,
            "skip_permissions": skip_permissions,
        })

    return server


async def tracked_call(name: str, args: dict, *, source: str = "mcp") -> ToolResult:
    """Run a tool call inside an attributed Run row so every direct MCP/CLI
    invocation gets history and non-NULL audit linkage, same as orchestrated
    runs. DB bookkeeping failures never block the tool itself."""
    run_id: int | None = None
    try:
        run_id = db.start_run(goal=f"tool:{name}",
                              dry_run=get_settings().guardrails.dry_run_default,
                              provider=source)
    except Exception:
        run_id = None
    token = set_current_run(run_id)
    try:
        result = await registry.call(name, args, run_id=run_id)
    except GuardrailViolation:
        _finish(run_id, "denied")
        raise
    except Exception:
        _finish(run_id, "failed")
        raise
    finally:
        reset_current_run(token)
    _finish(run_id, "ok" if result.ok else "failed", dry_run=result.dry_run)
    return result


def _finish(run_id: int | None, status: str, dry_run: bool | None = None) -> None:
    if run_id is None:
        return
    try:
        db.finish_run(run_id, status, dry_run=dry_run)
    except Exception:
        pass


async def _call(name: str, args: dict) -> str:
    try:
        result = await tracked_call(name, args)
        return result.model_dump_json()
    except GuardrailViolation as e:
        return f"[GUARDRAIL DENIED] {e}"
    except ToolError as e:
        return f"[TOOL ERROR] {e}"
    except Exception as e:  # keep the MCP session alive
        return f"[ERROR] {type(e).__name__}: {e}"


def main() -> None:
    try:
        db.sweep_orphaned_runs()
    except Exception:  # a broken audit DB must not block the server
        pass
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
