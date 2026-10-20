---
name: Bug report
about: Something does not work as documented
title: ""
labels: bug
assignees: ""
---

**What happened**

<!-- The command you ran and what it did instead. -->

**What you expected**

**Environment**

```
vtai version : <vtai info | head -1>
os           : <uname -a / Windows version>
python       : <python -V>
uv           : <uv --version>
provider     : <local | anthropic | openai>
transport    : <stdio | http>
```

**Config**

<!-- The relevant parts of your settings.toml — the guardrails block and
     paths, with secrets removed. `vtai info` prints the file in use. -->

```toml
```

**Logs / audit**

<!-- `vtai audit --denied --limit 20` is usually the fastest way to see
     what the guardrails thought. -->

```
```

**Anything else**

<!-- Screenshots, stack traces, links. -->
