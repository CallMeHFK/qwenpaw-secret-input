#!/usr/bin/env bash
# 发布包：唯一顶层目录 secret-input/，只带运行文件（spec section 6 / 13.5）。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="${root}/.build/secret-input"
version="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "${root}/plugin.json" | head -1)"
[ -n "${version}" ] || { echo "FATAL: cannot read version from plugin.json" >&2; exit 1; }

# symlink 非空即拒绝构建：zip 解压不重建链接，成员会静默变成"内容为目标路径的普通文件"
if [ -n "$(find "${root}/backend" "${root}/ui" -type l -print -quit)" ]; then
  echo "FATAL: symlink found in runtime tree; refuse to build" >&2
  find "${root}/backend" "${root}/ui" -type l >&2
  exit 1
fi

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
unzip -l "${out}" | tail -3
