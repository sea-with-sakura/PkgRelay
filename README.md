# PkgRelay

内网 Conda / pip 包缓存中转站。Conda 和 pip 继续负责依赖解析与版本选择；PkgRelay 负责回源、去重缓存和内网分发。

缓存只有两个池：`conda` 与 `pip`。不同上游只是来源记录，不复制同内容的包。

## 启动

```bash
cp config.example.yaml config.yaml
docker compose up -d --build
curl http://127.0.0.1:45612/healthz
```

服务默认监听 `0.0.0.0:45612`，网页为 `http://<服务器IP>:45612/`。
缓存数据默认绑定在宿主机 `/4090data1/pkgrelay/data`，便于迁移和备份。

## 客户端

每台机器上的每个用户执行：

```bash
wget -qO /tmp/setenv.sh http://172.16.8.251:45612/bootstrap/setenv.sh && bash /tmp/setenv.sh && exec bash -l
```

之后照常使用：

```bash
conda create -n demo python=3.12
pip install gpustat
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

显式的 HTTPS `--index-url` 会自动转向 PkgRelay。首次下载时网关边回源、边传输、边写入缓存；后续请求命中中央缓存。

安装客户端后，网关会接管当前用户的 `~/.condarc`：Channel 列表、优先级和公共源均由网关下发，用户原有配置不参与 Conda 解析。原文件会备份；选择卸载即可原样恢复。中转站优先使用清华 TUNA 镜像，失败时回退官方源。

需要同步并清理当前用户下载缓存时：

```bash
~/.local/share/pkgrelay/cache-sync.sh --prune
```

## 导入已有缓存

在缓存服务器上运行，不会修改原目录：

```bash
./import/import-cache.sh conda /home/sakura/miniconda3/pkgs
./import/import-cache.sh pypi /home/sakura/.cache/pip
```

可加 `--dry-run` 预览。生产环境建议使用 TLS，并只向可信内网开放端口。

## 重置旧缓存

本次架构不迁移旧的动态 Conda 路由。确认不再需要旧缓存后，在服务端执行：

```bash
./reset-cache.sh --yes
./rebuild.sh
```

该脚本只删除宿主机 `/4090data1/pkgrelay/data` 内的缓存和数据库。
