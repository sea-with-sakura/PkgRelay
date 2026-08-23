# PkgRelay 客户端

执行服务端提供的安装脚本：

```bash
wget -qO /tmp/setenv.sh http://cache.internal:45612/bootstrap/setenv.sh
bash /tmp/setenv.sh
exec bash -l
```

之后直接使用 `conda`、`pip` 或 `pip3`。显式 HTTPS `--index-url` 会自动经由 PkgRelay，不需要为 CUDA 等额外源手动配置映射。

`python -m pip` 不经过 Bash 函数；请使用 `pip` 或 `pip3`。
