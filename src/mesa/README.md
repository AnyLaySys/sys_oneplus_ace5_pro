# Mesa 26.2.4 / Adreno 830

`upstream.json` 固定上游版本与提交，`overlay` 保存修改后的两个源文件：

- `freedreno_devices.py`：匹配主线内核返回的 A830 修订号和 speed bin。
- `wsi_common_display.c`：检查 KMS 能力，避免对仅提供渲染的设备查询显示接口。

`dependencies.json` 固定 ARM64 依赖包的版本、下载地址和 SHA512。`cross.ini` 中的 `@SYSROOT@` 在构建时替换为当前工程路径。

```sh
python3 src/mesa/build.py
```

所有下载、上游检出、编译目录与 `.deb` 均写入 `build`。修改本目录源码后，脚本会重新构建软件包；输入未变时复用已生成的软件包。
