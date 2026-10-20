#!/usr/bin/env bash
# 发布包：唯一顶层目录 secret-input/，只带运行文件（spec section 6 / 13.5）。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for tool in python3 zip unzip; do
  command -v "${tool}" >/dev/null 2>&1 \
    || { echo "FATAL: required tool not found: ${tool}" >&2; exit 1; }
done

# symlink 非空即拒绝构建：zip 解压不重建链接，成员会静默变成"内容为目标路径的普通文件"。
# plugin.json 也是运行时输入，必须在读取版本前与两个目录一起扫描，否则会读并打包外部内容。
links="$(find "${root}/backend" "${root}/ui" -type l -print)"
if [ -L "${root}/plugin.json" ] || [ -n "${links}" ]; then
  echo "FATAL: symlink found in runtime tree; refuse to build" >&2
  [ -z "${links}" ] || printf '%s\n' "${links}" >&2
  if [ -L "${root}/plugin.json" ]; then
    printf '%s\n' "${root}/plugin.json" >&2
  fi
  exit 1
fi

# 版本必须取 JSON 顶层键：sed 取首个 "version" 行会被嵌套同名键劫持，
# 构建出 `secret-input-<nested>.zip` 却仍 rc=0。
version="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        manifest = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit(f"FATAL: cannot read plugin.json: {exc}")
version = manifest.get("version")
if not isinstance(version, str) or not version.strip():
    sys.exit("FATAL: plugin.json has no non-empty string version")
print(version)
' "${root}/plugin.json")"

stage="${root}/.build/secret-input"
rm -rf "${root}/.build"
mkdir -p "${stage}/backend" "${stage}/ui"
cp "${root}/plugin.json" "${stage}/plugin.json"
cp "${root}/backend/"*.py "${stage}/backend/"
cp "${root}/ui/index.js" "${stage}/ui/index.js"
find "${stage}" -name '__pycache__' -type d -prune -exec rm -rf {} +

mkdir -p "${root}/dist"
out="${root}/dist/secret-input-${version}.zip"
rm -f "${out}"
( cd "${root}/.build" && zip -qr "${out}" secret-input -x '*/__pycache__/*' )
rm -rf "${root}/.build"

echo "built ${out}"
unzip -l "${out}" | tail -3 || true  # 仅回显；清单失败不否定已完成的构建
