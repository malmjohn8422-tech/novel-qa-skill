# Write 权限配置

入库流程需要写入切块、summary、缓存和高层文档。spawn 出来的子 agent 继承当前 Claude Code session 的有效 permission 配置；如果当前运行目录的项目级 settings 没放开写入,worker 调 `Write` 会被拒。

优先检查实际运行入库命令的当前工作目录:

```text
<当前工作目录>/.claude/settings.local.json
```

个人使用时,最省心的项目级配置:

```json
{
  "permissions": {
    "allow": [
      "Skill(小说改良版)",
      "Bash(python *)",
      "Bash(PYTHONIOENCODING=utf-8 python *)",
      "Read(**)",
      "Write(**)",
      "Edit(**)"
    ]
  }
}
```

这是项目级配置,只影响在当前工作目录启动的 Claude Code session。不要把带 `Read(**)` / `Write(**)` / `Edit(**)` 的 `.claude/settings.local.json` 上传或发给别人。

如需收窄权限,把 `Read(**)` / `Write(**)` / `Edit(**)` 改成当前小说工作目录和 skill 目录的具体路径。Windows 路径分隔符在 JSON 里要 `\\` 双写。

如果希望所有项目默认生效,可把同类规则放到用户级:

```text
~/.claude/settings.json
```

但如果当前工作目录自己的 `.claude/settings.local.json` 更严格或有 `deny`,仍然要改当前工作目录的项目级 settings。

如果第一批 worker 报 `FAILED reason=Write permission denied`,说明配置没生效。可临时放宽为 `Write(**)`,或跳过子 agent,改为 main thread 顺序处理。
