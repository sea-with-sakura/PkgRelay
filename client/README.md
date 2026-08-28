# PkgRelay 客户端

执行服务端提供的安装脚本：

```bash
wget -qO /tmp/setenv.sh http://cache.internal:45612/bootstrap/setenv.sh && bash /tmp/setenv.sh && exec bash -l
```

之后直接使用 `conda`、`pip` 或 `pip3`。显式 HTTPS `--index-url` 会自动经由 PkgRelay，不需要为 CUDA 等额外源手动配置映射。

网关路由必须使用 `pip` 或 `pip3`。不支持 `python -m pip` / `python3 -m pip`：它们不会经过 Bash 包装器，无法保证中枢缓存、自动升级和版本门禁。

安装会备份并替换当前用户的 `~/.condarc`，由网关统一控制 Conda Channel；卸载会恢复原文件。
