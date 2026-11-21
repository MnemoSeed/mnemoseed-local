# 分片 390-legacy-dream-route-slices v3

> 状态: DRAFT / DESIGN_PENDING。配合 design v3 阅读。前置: PR #432 合并(G0 门:合并 HEAD 重取 configwrite/service.py 并核验 set_many 语义)。
> 相对 v2 变更: C1 检测 helper 打印新 repair 动词命令(不再打印两条 HTTP config set);新增 C2 进程内本地写动词与 C3 三形状隔离 oracle;C4 做 #432 重基线与 denylist 核验;AC4 由两形状扩为三形状(driver-only / model-only / both-missing)。

## 分片表
| 片 | 产物(写集) | 依赖 | 验收门 | 备注 |
| C0 legacy fixtures | tests/fixtures/legacy_driver_only.toml + legacy_model_only.toml + legacy_both_missing.toml | 无 | AC4 | 三形状均含 boot 需要的 storage 段 |
| C1 CLI 形状识别 | cli.py:共享 helper _legacy_route_migration_hint(exc);在 :274/:325/:656 及 up/on/doctor 用户可达 catch 点调用;打印一条 repair 命令(占位符 shell-safe) | C0(测试) | AC1/AC3/AC6/AC8 | 其它 ConfigError 原样;捕获后零写入 |
| C2 进程内本地写 | cli.py:新动词 config repair-dream-route(--role/--driver/--model);sanitize 临时文件 + load_config(temp) + cfg.source=real + build_stores + ConfigWriteService.set_many 两键 + close + load_config(real) 自检 | C0,#432 合并(G0) | AC2/AC4/AC7 | 不启动/连接 daemon;不引用 rest_client 或 HTTP config-set |
| C3 isolated oracle | 新文件 tests/test_legacy_dream_route.py:临时 home + spare port;三形状 -> 命令 -> 断言无 daemon 监听/进程 -> 执行 -> config.toml 两键字节 + meta DB 两记录断言 -> boot;覆盖 up/on/doctor;HOME 全树字节快照 | C0+C1+C2 | AC5;红相见下 | 不碰 7788 |
| C4 denylist verify | 无编辑:合并 HEAD 哈希 + git diff + grep 断言 | C1+C2+C3 | AC7:config.py/service.py diff 空;迁移 helper 无 HTTP 引用 | G0 重基线门在此关闭 |
## 合并顺序与提交
- 单批 C0+C1+C2+C3+C4 -> 一次提交(issue -> branch -> PR,Closes #390)。
- 合并置于 #432 之后(G0 门:复用 reload guard 与 set_many);与其它 Wave 并行工作树。

## 红相要求(TDD)
1. 先写 C3,无 C1/C2 时 RED(裸错误、无迁移命令、无可执行动词)。
2. C2 mutant: 用两次单键 set() 代替 set_many -> both-missing 与 driver-only RED(cross_validate 拒首写)。
3. C2 mutant: 把写路径改走 HTTP config set -> RED(DaemonUnavailableError,无 daemon),证明 daemon-independent 机制。
4. C2 mutant: 给残缺表填固定默认 model 而非删除整表 -> RED(AC7 无默认模型常量 + 真实文件字节只变两键断言)。
5. C2 mutant: cfg.source 指向临时文件而非真实路径 -> RED(真实 config.toml 字节不变断言)。
6. C1 mutant: 扩大捕获到其它 ConfigError -> AC6 RED。
7. C1 mutant: 检测阶段就写配置(未显式执行 repair) -> AC3 HOME 全树快照 RED。
8. C1 mutant: 只在单一 catch 点实现 -> AC5 的 doctor/up 分支 RED。
9. 每红相留 raw 失败行 + 字节精确恢复证据。
