# Sys for OnePlus Ace 5 Pro

OnePlus Ace 5 Pro 的 Ubuntu 26.10 ARM64 系统构建工程。

## 目录

* `build.py`：系统镜像构建入口。
* `cfg.toml`：内核、UEFI 输入与系统配置。
* `src/init`：initramfs 启动源码及模块清单。
* `src/mesa`：Mesa 固定版本、修改源码、ARM64 依赖清单与打包脚本。
* `src/\*firmware`、`src/qcom-sm8750-audio`：现用固件与音频拓扑。
* `src/wifi-wcn-reference`：Wi-Fi 固件及适配参考源码。
* `build`：下载、第三方源码检出、编译缓存和产物。

## 构建环境

使用 Ubuntu 26.10 x86\_64 / WSL，安装构建工具：

```sh
sudo apt install build-essential git python3 gcc-aarch64-linux-gnu g++-aarch64-linux-gnu libc6-dev-arm64-cross clang lld llvm bc libelf-dev libssl-dev kmod e2fsprogs android-sdk-libsparse-utils qemu-user qemu-user-binfmt ubuntu-keyring meson ninja-build pkgconf bison flex glslang-tools libwayland-bin python3-mako python3-yaml python3-packaging
```

在本工程同级准备内核与 UEFI 工程，并按 `cfg.toml` 提供构建产物：

* 内核：[https://github.com/AnyLaySys/linux\_oneplus\_ace5\_pro](https://github.com/AnyLaySys/linux_oneplus_ace5_pro)
* UEFI：[https://github.com/AnyLaySys/uefi\_oneplus\_ace5\_pro](https://github.com/AnyLaySys/uefi_oneplus_ace5_pro)，默认目录为 `Mu-Silicium`。

内核首次构建：

```sh
cd ../linux\_oneplus\_ace5\_pro
make O=out/kernel ARCH=arm64 LLVM=1 LLVM\_IAS=1 oneplus\_ace5\_pro\_defconfig
python3 ./build.py -j $(nproc)
```

## 系统镜像

在本工程目录执行：

```sh
./build.py
./build.py -r build/ubuntu-时间戳
./build.py -nr
```

依次为全新制作系统、复用指定 rootfs 缓存、仅生成 boot。产物默认保存至 `build/out/时间戳`，末尾可追加输出目录。

生成 rootfs 时会自动构建并安装 `mesa-sm8750`。修改后的 Mesa 源码在 `src/mesa/overlay`，构建时覆盖到固定版本的上游源码。单独构建 Mesa：

```sh
python3 src/mesa/build.py
```

软件包输出为 `build/mesa-sm8750.deb`。Mesa 构建使用固定的 ARM64 开发包与运行库，不依赖手机 SSH 或已有 rootfs。

## 源码保存

提交根目录构建文件和完整 `src`。`src` 中的现用固件、音频拓扑和修改源码必须一并保存。

`build` 已被 Git 忽略。Mesa 修改应编辑 `src/mesa/overlay`；`build/src/mesa` 是可重新生成的源码检出。内核和 UEFI 修改分别保存在各自的仓库。

