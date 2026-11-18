# Clash Verge 监控器 1.3.3

适用于 Ubuntu 的中文桌面监控器，支持 Clash Verge Rev。仓库仅保留 1.3.3。

## 功能

- 一键发现本机连接，支持回环 HTTP 和服务模式 Unix 套接字。
- 跟随当前代理节点，主、备用地址双重测速。
- 托盘显示状态颜色和实测延迟，支持登录自动开始监控。
- Clash 退出后自动启动，默认开启。
- 连续故障后依次执行两轮 TUN 恢复、内核重启、两轮 TUN 恢复、软件重启。
- 软件重启后仍失败，保持 TUN 开启，休息 60 秒后自动恢复监控。
- 操作次数限制、持久化记录和日志脱敏。

## 安装与升级

下载 [1.3.3 安装包](release/clash-verge-watchdog_1.3.3_all.deb)，在文件所在目录运行：

~~~bash
sudo apt install ./clash-verge-watchdog_1.3.3_all.deb
~~~

升级前从托盘退出监控器。已有 deb 安装可以直接升级，设置位于
~/.config/clash-verge-watchdog/。已有开关设置优先于新版本默认值。
打开程序，点击“一键配置连接”，选择代理组后开始监控。
登录自启动可在首页开启。主动退出 Clash 前应先关闭其自动启动开关。

如果以前使用用户目录或源码安装，旧快捷方式可能仍指向旧程序。
用以下命令核实并启动系统安装版本，然后在新版重新设置登录自启动：

~~~bash
/usr/bin/clash-verge-watchdog --version
/usr/bin/clash-verge-watchdog
~~~

## 开发与打包

~~~bash
sudo apt install python3 python3-tk python3-gi gir1.2-glib-2.0 gir1.2-ayatanaappindicator3-0.1
python3 -m clash_watchdog
python3 -m unittest discover -s tests -v
python3 packaging/build_release.py --output dist
~~~

测试需要桌面会话才能覆盖 GUI。实机验证环境为 Ubuntu 26.04 ARM64。
其他系统与架构的验证范围见 [验证记录](VALIDATION.txt)。
详细行为和恢复流程见 [使用说明](README.txt)。

## 隐私

连接密钥位于当前用户私有设置目录，设置和日志不应上传或随安装包分享。
仓库与发行包不包含个人配置、订阅或日志。
