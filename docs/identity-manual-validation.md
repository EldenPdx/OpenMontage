# 身份集成的人工安全验收

状态：**PENDING**。本清单按用户 2026-10-13 的要求保留，由用户手工执行；开发 Agent 不运行或自动派发此套测试。正常登录、成员与角色操作、制作审批、视频播放下载、迁移和正向备份恢复继续自动验证。普通浏览器断连、已授权请求的响应丢失和同空间重试也继续自动验证。

## 推荐执行入口

1. 打开仓库的 [CI 工作流](https://github.com/EldenPdx/OpenMontage/actions/workflows/ci.yml)，选择 **Run workflow**。
2. 选择已经整合全部身份改动的候选分支或标签。记录该候选完整 Git SHA。
3. 显式勾选 **manual_security**，然后手工启动。默认不勾选；PR/push CI 始终选择普通自动套件。
4. 等待工作流结束，下载 `required-identity-outcomes` artifact，检查 `outcomes.json` 的 `candidate` 与预期 SHA 完全一致。
5. 将工作流链接、候选 SHA、结果文件和失败节点反馈给整合负责人。失败需要修复并重跑，不能以跳过、mock 或测试数量替代验收。

人工套件使用全新的隔离 CI runner，启动固定版本的 Logto、标准 IdP、PostgreSQL、邮件、Pi 和浏览器；它不部署生产。无需把本地凭据填进仓库或工作流输入。

## 待人工执行的项目

| 项目 | 必须保留的验证 |
| --- | --- |
| OIDC/SAML 声明与签名 | 有效正向对照；issuer、audience、nonce、请求关联、recipient、有效期及签名不满足要求时，在正确边界拒绝，且不产生部分会话 |
| 回调、授权码和已签断言复用 | 并发只有一次成功；重复提交不会新增 Native/BFF 会话或取得资源 |
| 不同 IdP 的同邮箱身份 | 不继承已有用户的身份、成员权限或资源 |
| 伪造 cookie/CSRF/Host/origin/proxy/actor/邀请输入 | 返回原要求的拒绝状态，且没有业务变更；包含 UI 故意改写 CSRF 的场景 |
| 密文、文件路径、符号链接、HTML/SVG | 不接受替换会话，不逃出允许的文件范围，不执行活动脚本；恢复后的错误会话密钥仍被拒绝 |
| 身份、数据库和供应商故障注入 | 真实停服、阻塞读取、Native Management 响应损失、邮件限流、权限模板异常及全局依赖故障校准，保留原有关闭边界和恢复断言 |

精确函数和参数化节点由 [manual-security.json](../auth-runtime/manual-security.json) 与 [required-nodes.json](../auth-runtime/required-nodes.json) 管理。当前总计 420 个必需节点：285 个自动、135 个人工；原有 363 个必需节点全部保留。人工清单还包含独立的 `dependency-failure-calibration` 操作。

## 本地操作者入口

只在拥有独立身份栈和隔离业务数据库的完整验证环境使用；运行器会拒绝另一 checkout 拥有的身份服务。使用与 CI 一致的已安装依赖、官方 Pi、媒体工具、Chromium、私有角色配置及会话密钥。先查看选择范围，这两个命令不访问服务或执行测试：

```sh
.venv/bin/python -B auth-runtime/ci.py plan
.venv/bin/python -B auth-runtime/ci.py plan --suite manual-security
```

完成独占环境准备后，由用户手工执行：

```sh
.venv/bin/python -B auth-runtime/ci.py run --suite manual-security --candidate "$(git rev-parse HEAD)"
.venv/bin/python -B auth-runtime/ci.py publish
```

`run` 需要干净的精确候选及完整 CI 环境。共享开发身份栈不能作为新 runner 的替代；备份恢复与故障窗口由其唯一操作者负责。人工套件的结果守卫要求每个已选节点的 setup/call/teardown 均成功，任何 skip/xfail/缺失节点都失败。

## 如何读取结果

人工套件全部完成时，`manual_security_passed` 为 true，`manual_pending` 和 `manual_pending_operations` 都为空。自动套件的 `automated_passed` 单独记录。两个工作流在不同 runner 运行时，应在最终审查中按候选 SHA 对齐各自结果。

`acceptance_status: PENDING` 和 `completed: false` 表示完整验收仍有未完成项；自动 CI 绿色不代表这些人工场景通过。独立复核、Issue 签收和两个套件的精确候选证据齐全后才能关闭 #30/#32/#22。原始日志、cookie、断言、密钥、数据库备份与私有配置继续保存在受保护的诊断目录；公开 artifact 只包含脱敏节点结果。

完整矩阵及启用条件见 [身份验证矩阵](identity-validation-matrix.md#automatic-verification-and-manual-security-acceptance) 和 [启用/恢复 runbook](identity-enable.md)。
