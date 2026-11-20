Clash Verge 监控器 1.3.4 · Ubuntu 版
应用版本：1.3.4
安装包修订：1.3.4（支持 Ubuntu 服务模式与一键配置连接）
目标系统：Ubuntu 26.04 桌面版 ARM64
发布日期：2026-11-14

一、安装与打开

推荐分享包：ClashVergeWatchdog-v1.3.4-Ubuntu-ARM64-Share.zip
右键解压分享ZIP，打开解压后的文件夹，双击 clash-verge-watchdog_1.3.4_all.deb。
在Ubuntu“应用中心 / App Center”点击“安装”，按系统提示输入账户密码并等待完成。
如果双击打开了压缩包管理器，请右键.deb →“打开方式 / 使用其他应用打开”→“应用中心”。
只解压外层ZIP，不要解压.deb。正常图形安装无需终端命令、额外脚本或设置可执行权限。
完整鼠标操作步骤见 QUICKSTART.txt 和 START-HERE.html（分享包内名为“安装说明.txt”和“开始使用.html”）。

安装包：clash-verge-watchdog_1.3.4_all.deb
文件名中的 all 表示程序本身不包含特定CPU架构的二进制，适用于ARM64；Ubuntu会安装本机ARM64的Python/Tk/GI依赖。

仅供习惯终端的用户：在安装包所在文件夹运行
sudo apt install ./clash-verge-watchdog_1.3.4_all.deb

安装完成后，在Ubuntu应用菜单搜索“Clash Verge 监控器”并打开。
也可以运行：clash-verge-watchdog
桌面程序使用普通用户运行，不需要sudo。首次安装依赖时需要联网。

依赖由Ubuntu仓库提供：python3、python3-tk、python3-gi、gir1.2-glib-2.0, gir1.2-ayatanaappindicator3-0.1。
没有pip安装步骤，没有后台开机服务，不会改写Clash配置。
便携源码包可以在安装相同依赖后运行 python3 -m clash_watchdog。

二、首次连接

1. 安装并启动Clash Verge Rev，使用当前Ubuntu桌面用户。
2. 打开监控器“连接设置 / 使用说明”页，点击“一键配置连接”。
   默认Clash配置目录为：~/.local/share/io.github.clash-verge-rev.clash-verge-rev
   如使用XDG_DATA_HOME或便携安装，请选择实际目录。
3. 返回“监控”页，选择当前使用的代理组。
4. 点击“检查重启入口”，确认D-Bus菜单可用，然后开始监控。

若Clash没有启用外部控制器，请在Clash设置中启用，监听地址设置为127.0.0.1:9097并设置密钥；
监控器填写 http://127.0.0.1:9097 和同一密钥。已有其他本机端口可以沿用。
读取配置按钮只读，不修改YAML、订阅、节点或系统代理。仅支持明确的本机回环地址。

三、保留的1.3.4行为

正常测速间隔：30秒。
失败复测间隔：5秒。
单地址测速超时：5秒。
连续5轮“双地址都失败”才请求重启内核。
启动、切换节点、恢复后的宽限期：30秒。
间隔从上一轮完成后计算，不包含请求自身的耗时。

主地址默认：https://www.gstatic.com/generate_204
备用地址默认：https://cp.cloudflare.com/generate_204
主地址失败、备用成功显示“部分连接异常”，清零连续失败，不触发重启。
控制器不可达、身份验证失败、无法解析响应、未知当前节点，不计为节点故障。
控制器未知状态保留已有失败，但不能用旧证据重启；长时间间隔后重新积累。

内核重启每滚动小时最多5次。未确认恢复时，重试退避为2分钟、5分钟、10分钟。
次数在派发前保存，失败或结果不确定也保留额度。额度到期自动解除，诊断继续。
派发结果不确定时暂停自动重启；确认Clash实际状态后点击“恢复自动重启”，不能绕过额度和冷却。

独立诊断正常、已启用的重启能力就绪时，顶部为深绿色。
部分异常、未知、宽限期或入口未就绪为深琥珀色；完整诊断失败为暗红色。
正常延迟只证明测速地址可达，不保证Codex或每个网站的连接正常。

四、Ubuntu原生重启方式

通过当前用户桌面会话的StatusNotifierItem / com.canonical.dbusmenu接口，
定位Clash Verge自己的“更多 → 重启 Clash 内核”，让Clash自身处理重启。
不会统计Windows式托盘图标数量，不依赖坐标、OCR或鼠标点击，也不会直接强杀进程。
核对当前用户、Clash主进程PID与创建时间、D-Bus唯一owner、菜单路径及准确菜单项。
主程序缺失、身份不明、多个实际目标或菜单无法确认时，会显示原因并暂缓恢复。
实现针对Clash Verge Rev 2.5.10官方菜单协议；其他版本必须通过实际入口检查，不能据此保证兼容。

Ubuntu版显示“内核历史参考”，并明确区别于Clash窗口当前读数。
目前没有读取/覆盖Clash窗口延迟缓存的接口；本版不提供Windows UI Automation的界面读取与强制刷新。
缓存历史不参与失败累计。此差异不影响独立双地址诊断。

五、Clash主程序退出后自动启动

独立开关，默认开启；开始监控后才生效。
每5秒检查本机进程，持续缺失至少10秒后尝试启动已配置的程序。
至少间隔2分钟、每滚动小时最多3次，额度到期继续。记录持久化，不因重开监控器而清空。
已有进程、进程状态不明、路径无效或次数保存失败时，不重复启动。
这不是网络测速，不增加外网请求。与内核重启的5次/小时额度分开。
无法可靠区分崩溃与主动退出：准备主动退出Clash时，先取消开关或停止监控。
启动请求成功后仍等待新的接口和测速结果确认恢复，不继承旧失败立即重启。

托盘可用时关闭窗口会继续监控；通过托盘菜单打开窗口或退出。灰色表示未监控，绿色正常，黄色等待或需要关注，红色表示双地址测速失败。
可在首页勾选登录时自动监控；登录后自动收进托盘。

六、设置、日志与自检

设置和日志：~/.config/clash-verge-watchdog/
如设置XDG_CONFIG_HOME，使用其下的clash-verge-watchdog目录。
settings.json包含连接密钥和两套恢复次数；目录权限700、文件权限600，仅当前用户可访问（系统管理员除外）。
密钥未加密，使用Ubuntu用户文件权限保护，不应分享settings.json。密钥不写入监控日志。
日志每个文件最多2MiB，保留2个轮转备份；记录会话、请求、完整诊断、恢复和自动启动结果。
Windows版DPAPI配置不能直接复制使用；Ubuntu上读取本机Clash配置即可。

只读自检命令（不会启动、重启或关闭Clash）：
clash-verge-watchdog --self-check

自检会输出系统架构、Python/Tk、主程序检测结果和原生重启入口状态，不输出密钥。
诊断问题时可以提供自检结果与watchdog.log。请不要发送settings.json。

七、验证范围

已在 Ubuntu 26.04 ARM64、Python 3.14、Wayland 验证界面、托盘、真实测速与完整恢复流程。
应用中心图形安装、其他 Ubuntu 版本和其他 CPU 架构尚未完成实机验收。
测试数量、已覆盖场景及未验证项见VALIDATION.txt。安装后应先做“连接并加载”和只读“检查重启入口”。

移植依据：
Ubuntu 26.04 python3-tk：https://packages.ubuntu.com/resolute/python3-tk
Ubuntu本地deb安装：https://ubuntu.com/server/docs/how-to/software/package-management/
Ubuntu26.04图形安装本地deb：https://ubuntu.com/desktop/docs/en/26.04/explanation/snap-and-deb-packages/
Clash Verge菜单：https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.10/src-tauri/src/core/tray/mod.rs
Clash Verge配置目录：https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.10/src-tauri/src/utils/dirs.rs


分级恢复（1.3.4）
连续失败达到阈值后，最多两次 TUN 关闭/开启循环。确认关闭后等待 1 秒再开启，
确认开启后等待 2 秒进行双地址诊断，失败则在该诊断结束后再等待 2 秒复测。
仍失败则重启内核，确认就绪后诊断两次；仍失败再进行两次 TUN 循环。
最后通过 Clash 自身菜单重启软件一次。软件就绪且 TUN 开启后两次诊断仍失败，
保持 TUN 开启，暂停测速和恢复操作 60 秒。倒计时结束立即重新诊断并清零连续失败计数。
休息期间每 5 秒核实 TUN 状态，无法确认时先恢复开启，再开始休息倒计时。
恢复额度保留：整轮恢复每小时最多 5 次且间隔至少 120 秒，软件重启每小时最多 2 次，
内核重启沿用设置中的额度和退避。额度用完后继续正常诊断。
退出时若正在恢复，等待 TUN 收尾完成；异常退出留下的待恢复标记会在再次开始监控时处理。
