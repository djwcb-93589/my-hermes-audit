# AuditSandbox 隔离与清理

## P4 Variant 维度

旧 Case 仍使用 `<root>/<run>/<case>/<trial>-<sandbox-id>/`。只有显式 P4 Variant 使用 `<root>/<run>/<case>/<variant>/<trial>-<sandbox-id>/`，并在 Manifest 记录 `variant_id`。因此不同 Variant 不共享 workspace、`HERMES_HOME`、SQLite、Memory 文件、Artifact 或日志；Session ID 还在 Worker 请求中按 Variant 稳定命名空间化。清理仍只删除通过 ownership marker 校验的当前 Trial 根，并按从深到浅顺序尝试移除空的 Variant/Case/Run 父目录。

## 布局

每个 `AuditSandbox` 属于一个 `(run_id, case_id, trial_number)`，并增加随机 `sandbox_id` 防止冲突：

```text
<controlled-root>/<run-id>/<case-id>/<trial>-<sandbox-id>/
├─ .myhermes-audit-owned.json
├─ manifest.json
├─ hermes_home/
├─ workspace/
├─ database/
│  └─ hermes.db                 # 仅保留路径，不初始化文件或 schema
├─ artifacts/
├─ fixtures/
└─ logs/
```

未传 `base_dir` 时使用 `tempfile` 创建受控根；显式 `base_dir` 由调用方选择。Sandbox 不修改 `os.environ`。

## 环境覆盖

`environment_overrides()` 返回新字典：

- `HERMES_HOME`；
- `DB_PATH`；
- `HERMES_WORKSPACE`；
- `MYHERMES_AUDIT_ARTIFACTS_DIR`。

未来 runner 决定如何把这些值传给子进程。ExecutionSpec 自带的环境 override 不能替换 runner 对隔离路径的最终控制。

## 路径安全

合同和运行时都拒绝：

- 绝对路径、UNC、Windows 盘符或 drive-relative 路径；
- `..`、`.`、空段、反斜杠、NUL、控制字符和 NTFS alternate-stream 冒号；
- Fixture 写到 `workspace/` 与 `hermes_home/` 之外；
- 解析后因现有符号链接逃逸的目标；
- 符号链接源文件与符号链接目标文件。

`copy_fixture_file()` 与 `write_fixture_content()` 只有在 Sandbox 已创建后可用。默认不覆盖已有目标。YAML loader 本身不会调用这两个方法。

## Manifest 与清理

`manifest.json` 只含 schema version、sandbox/run/case/trial 身份、UTC 创建时间和相对布局，不含环境变量、API key、owner token 或其他凭据。

所有权 token 只存在实例内存和独立 marker。默认上下文退出时，清理逻辑会重新验证：

1. 根目录不是符号链接；
2. 根目录仍位于原受控根下，且不等于受控根；
3. marker 是普通文件；
4. marker signature 与 owner token 都匹配。

任一条件失败都会拒绝删除。清理只递归删除当前 Trial 根；`preserve=True` 会跳过自动清理，调用方仍可显式调用 `cleanup()`。

这些检查是应用层文件边界，不等同于操作系统、容器或虚拟机安全隔离。未来执行不可信 Agent 时，runner 仍需选择适当的系统级沙箱。
