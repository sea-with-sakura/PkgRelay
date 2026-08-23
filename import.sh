cd "$(dirname -- "$0")"

# 先预览
./import/import-cache.sh conda /4090data1/sakura/miniconda3/pkgs --dry-run
./import/import-cache.sh pypi /home/sakura/.cache/pip --dry-run

# 确认后正式导入
./import/import-cache.sh conda /4090data1/sakura/miniconda3/pkgs
./import/import-cache.sh pypi /home/sakura/.cache/pip
