# Mac 打包环境准备

> 在 macOS 上构建 AppColdStart.dmg 安装包的操作指南。

---

## 一、环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | macOS 12+（Monterey 或更新） |
| Node.js | 18+（https://nodejs.org） |
| Python | 3.11+（`brew install python@3.11`） |
| Git | 已安装（`git --version` 验证） |
| 磁盘空间 | 至少 2 GB |

---

## 二、安装步骤

### 1. 装 Node.js

```bash
brew install node
node --version    # 确认 v18+
```

### 2. 装 Python

```bash
brew install python@3.11
python3 --version  # 确认 3.11+
```

### 3. 装 scrcpy（镜像功能用）

```bash
brew install scrcpy
```

### 4. 装 iOS 工具链（测 iOS 用）

```bash
brew install libimobiledevice
brew install ideviceinstaller
```

---

## 三、拉代码

```bash
git clone git@git.7k7k.com:tianhaishun/app-coldstart.git
cd app-coldstart
git checkout master
```

---

## 四、装项目依赖

```bash
npm install
```

---

## 五、打包

```bash
npm run build:mac
```

产出在 `release/` 目录下，是一个 `.dmg` 文件。

---

## 六、注意事项

### Mac 版和 Windows 版的区别

| 功能 | Windows | Mac |
|------|---------|-----|
| 内嵌 Python | ✅ 已内置 | ❌ 需自己装（brew install python） |
| 内嵌 ADB | ✅ 已内置 | ❌ 用系统 adb（brew install android-platform-tools） |
| 内嵌 scrcpy | ✅ 已内置 | ❌ 需自己装（brew install scrcpy） |
| 内嵌 iOS 工具 | ✅ 已内置 | ❌ 需自己装（brew install libimobiledevice） |
| 源码保护 | ✅ 构建时编译（内嵌 Python 固定版本） | ✅ 首次启动时运行时编译（用实际运行的 Python，版本永远匹配） |

Mac 用户安装 .dmg 后首次启动，工具会自动创建 Python 虚拟环境并安装依赖（需要联网，约 2-3 分钟）。

### 首次启动签名提示

Mac 版没有开发者签名，首次打开可能提示「无法验证开发者」。解决方式：

```bash
# 方式一：右键 → 打开（推荐）
# 方式二：终端去除隔离属性
xattr -cr /Applications/AppColdStart.app
```

### 代码签名（可选）

如果有 Apple 开发者证书，可以在 `package.json` 中配置签名：

```json
"mac": {
  "identity": "你的开发者证书名",
  "hardenedRuntime": true,
  "gatekeeperAssess": false
}
```

不配也能打包，只是用户首次打开需要手动信任。

---

## 七、验证清单

打包完成后检查：

- [ ] `release/` 目录下有 `.dmg` 文件
- [ ] 双击 .dmg 能打开安装界面
- [ ] 拖到 Applications 后能启动
- [ ] 启动后能自动创建 Python 虚拟环境
- [ ] 能检测到 USB 连接的设备
