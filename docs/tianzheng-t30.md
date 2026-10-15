# 天正 T30 MCP 接口

新增 `tianzheng` 工具直接通过 AutoCAD ActiveX 调用天正完整命令名，
不使用鼠标、键盘模拟，也不开放任意 LISP、命令文本或宏。

## 命令目录

目录来源为本机 T30-Arch V1.0 Build241230 的 `SYS/tchcmd.txt`、`acad.pgp`，
以及随软件安装的 757 页官方使用手册。原命令表包含453个名称，已补充安装选项中的47个名称，共500个。
补充条目明确标记语义未确认状态；手册节号、页码是证据线索，不能代替实测。
页码采用 PDF 页序。安装命令表的疑似拼写错误不自动修正。

- `catalogue`: 按命令名、中文名称、含义或分类搜索，支持分页。
- `describe`: 返回完整命令名、中文名称、别名、含义、手册页码和限制。
- `inspect`: 查询活动命令及模型空间对象的句柄、类型和包围盒。
- `registry`: 分页检查 CAD 内实际命令登记，只查询，不运行候选命令。
- `walls`: 只读。`data={insert_handles:[...], max_depth?}`，逐级遍历 INSERT 的块定义（含嵌套块），读取 TDbWall 的 StartPoint/EndPoint/LeftWidth/RightWidth（弧墙另读 Radius/Bulge/Center），按各级插入点、旋转、缩放、镜像和法向变换到 WCS，返回中线、左右边线、墙厚、`mirrored` 与来源路径。
- `jobs`: 读取服务内任务元数据，不通过 COM 读取正在等待输入的 CAD。
- `draw_walls`: 用一串坐标自动绘制天正墙。
- `execute`: 用完整的已核对输入序列自动绘制墙体、插入门窗或创建带形窗。
- `execute`: 六个 ALX/ALY 对齐命令接受 `selection_handles`，独立核对包围盒位置、尺寸和另一方向的位置。
- `start`: 通过 PostCommand 异步启动登记过的命令，供人工填写参数或交互；返回 `job_id`。
- `input`: 旧版 AutoCAD 的分步输入；AutoCAD 2015 及以后拒绝此操作，要求使用完整 execute 序列。
- `status`: 查询任务是否完成，并核对实际新增的天正对象。
- `cancel`: 结束该任务自己的命令。已绘制对象保留；不声称事务回滚。

查询命令目录不需要连接 CAD。离线 ezdxf 后端不能执行天正命令。
图形导出、库操作、工程管理、外部文件转换等条目保留在目录中，
在获得能强制使用受管输出目录的适配器之前拒绝执行。

## 调用流程

先调用 `system(operation="ensure_ready")`。测试需新建图纸，并用
`transaction(operation="context")` 确认活动图纸、`doc_id` 和修订号。
每次 `start`、`execute`、`draw_walls`、`input`、`cancel` 前重新读取 context；这些操作必须携带
`doc_id`、`expected_revision` 与唯一 `idempotency_key`，并传递 context
提供的 `lease_token`、`worker_generation`。

```json
{"operation":"describe","data":{"command":"TGWall"}}
```

```json
{
  "operation":"start",
  "doc_id":"从 context 读取",
  "expected_revision":0,
  "idempotency_key":"为本次启动生成的唯一值",
  "data":{"command":"TGWall"}
}
```

自动绘制普通直墙使用一次完整的 `draw_walls` 调用：

```json
{
  "operation":"draw_walls",
  "doc_id":"从 context 读取",
  "expected_revision":0,
  "idempotency_key":"为本次绘图生成的唯一值",
  "data":{"points":[[0,0,0],[6000,0,0],[6000,4000,0],[0,4000,0],[0,0,0]]}
}
```

在墙上插入门窗使用 `execute`，`data` 为
`{"command":"TOpening","points":[[1800,0,0]]}`，同样携带最新图纸身份、
修订号与幂等键。尺寸、材料和门窗类型使用当前 T30 参数；不接受未实现的
width/height 设置。自动绘图目前要求世界坐标系。

AutoCAD 2025 的 SendCommand 会等待整条命令完成，因此坐标与终止输入
必须一次送出。`start` 使用 PostCommand 供人工交互，返回
`queued_for_manual_interaction`，不立即同步查询 CAD。CAD 等待选择时可能
拒绝 COM 调用，启动成功并不表示绘图完成。`jobs` 可在此时查询服务元数据；
实际状态和对象需在命令结束后核对。分开调用 `input`
不能用于新版 CAD 的自动绘图，也不会被接口偷偷替换成键盘模拟。
矩形标准柱可使用已验证的初始化样板复制配方。首次样板需要通过正常
天正命令创建；同一样板转换圆形、正六边形、正八边形也已通过限定参数实测。
其余异形截面、任意旋转及交互面板模式选择尚未适配。

输入类型为有限数值 `number`、二维/三维坐标 `point`、十六进制句柄列表
`selection`、已核对的命令选项 `keyword` 和不含值的 `enter`。
`selection` 的选择表达式由服务端从已验证的句柄生成，客户端不能传入代码。
不接受自由文本、文件路径或批量命令字符串。

## 语义与验证边界

`TGWall` 创建真正的天正墙；`TSWall` 将已有线对象转换成天正墙；
`TOpening` 在天正墙中插入门、窗或洞口；`TGColumn` 启动标准柱参数面板。
这些命令的参数面板可能是非模式对话框，尺寸、材料、墙高、门窗类型等
继续使用天正当前参数。接口不把未设置的对话框参数宣称为已生效。

`command_sent` 仅代表命令已提交。`completed` 表示命令交互结束，
`drawn` 表示新增对象，`geometry_verified` 根据实际对象的天正类型独立核对。
若自定义 COM 包装缺少可读类型，则返回 `unreadable_custom_class`，
不宣称已验证天正墙。`created_objects`、`modified_objects`、
`removed_handles` 提供结果证据。

已有 CAD 图形命令不是原生数据库原子事务。失败或取消可能保留先前已画
对象。出现 `E_TIANZHENG_SEND_UNCERTAIN` 不得换一个幂等键重发；先查询
任务状态与图纸 context。服务重启后旧交互任务不会恢复，调用方必须先
确认 CAD 的真实命令状态。

命令目录的存在不意味着500个命令的全部交互参数都经过实机验证。
完整自动化参数需要按各命令分别验证；目录用 `verified_live` 明确区分。
`execution_contract` 明确给出自动配方是否存在及必需参数。目前有32个经实测的自动配方，范围见下文。其他自动调用在触及 CAD 前返回
`E_TIANZHENG_RECIPE_UNAVAILABLE` 或 `E_TIANZHENG_ADAPTER_REQUIRED`。
手工启动不代表自动参数已适配。`cancel` 也可能受 CAD 交互期间 COM 拒绝
调用限制，不能把发送取消消息当作已结束；需检查实际状态。

对齐调用示例：`{"command":"ALXLEFT","selection_handles":["101","102"]}`。
不能同时传 points/wall_handles，句柄须属于当前模型空间。当前自动配方仅
支持世界坐标系。六个命令为 ALXLEFT、ALXRIGHT、ALXMID、ALYTOP、ALYBOTTOM、ALYMID。

## 本次实机验证

2026-10-14，Windows 11 ARM64 / AutoCAD 2025 / T30 Build241230。
MCP 与 CAD 均以 matincz 用户的管理员权限运行。

- 纯 MCP 完成四段墙与一个门窗对象的创建。
- ActiveX 类 TDbWall 与 LISP entget 的 TCH_WALL 类型对应；墙厚 240、墙高 3000。
- ActiveX 类 TDbOpening 与 LISP entget 的 TCH_OPENING 类型对应；尺寸 900 × 2100。
- 原项目图纸未写入；检测到活动图纸切换后测试脚本拒绝继续绘图。
- 重新启动CAD后，通过正常命令生成矩形柱样板，随后经MCP复制出两根600×400、高3300、标高0及200的柱，样板保持不变。
- 补充了通用 COM ProgID 缺失时的版本名称回退，并保留 PID 身份校验。

验证当前 T30 参数下的基本创建流程，不等于全部命令、模式或属性设置均已验证。

## 全量检查进度

2026-10-14 在独立 Drawing6 完成 453 个命令的注册查询：419 个当前已登记，
34 个未检出；后者可能是动态加载、旧版条目或命令表拼写问题，未直接判为不存在。
首次 ALXLEFT 启动暴露出交互期间同步 COM 查询错误，现已修复并实机验证。
六个对齐配方已在不同大小、不同位置的矩形上实际执行并通过几何检查；
TGWall、TOpening、TBanWin 也在同一图内重新验证，最终 22 个对象、活动命令为 0。
全量功能验证尚未完成，登记通过与不支持参数的拒绝检查均不计为功能通过。

## 带形窗与连续计时

TBanWin 的 execute 数据示例为 `{"command":"TBanWin","points":[[4000,10000,0],[6000,10000,0]],"wall_handles":["从 inspect 取得的墙句柄"]}`。两个位置应落在选择的天正墙上。仍须传递最新 doc_id、expected_revision 与幂等键。

2026-10-14 连续运行实测：新建空白图、八面墙、一扇普通门、一扇带形窗并核对对象，耗时 10.134 秒；包括保存 DWG 和 CAD 原生预览为 15.125 秒。该计时排除了之前的接口开发、研究与 HTTP 会话初始化。门通过 opening_kind=门 验证，带形窗通过 TDbBandWin/TCH_CORNER_WINDOW 验证。普通门不能通过 GetSubKind 属性可靠地直接转换为窗，未开放该转换接口。

## 重复验证

在已加载 T30 的 AutoCAD 上运行（端点按实际部署填写）：

```bash
python examples/tianzheng_validation.py --url http://10.211.55.4:8765/mcp --output validation.json
```

脚本先 ensure_ready，创建独立空白图，扫描全目录登记并验证全部 9 个已有
自动配方；每次修改重新读取图纸身份与修订号，活动图纸切换或任何错误均
立即停止，不自动重试崩溃或超时。该脚本只覆盖最初9项配方，其余491项未由该脚本实测，不声称全功能
通过；绘图完成后保存受管 DWG。启动前应让 CAD 空闲。

## 后续适配进度（2026-10-14）

原命令表漏掉了安装选项中的部分名称，现补充47项，共500项；补充名称不等于已确认的功能。经纠正两个名称前缀误匹配后，382项附加了正文节号和页码，修复了部分附录截断语义及错误分类。

当前32项配方通过实测：矩形标准柱和矩形样板复制、移位、自由移动、自由复制，以及原有3项创建、6项对齐，加上3项全局图层状态、3项选择图层锁定、反向、精确重复清理、4项隐藏显示、统一标高和5项属性修改。检查清单保留每项证据及未适配状态。

以下调用数据均使用 execute，仍须提供新 context、doc_id、expected_revision、worker_generation 与幂等键：

| 命令 | data 参数 |
|---|---|
| TopenAllLayer / TUnfreezeAllLayer / TSwitchAllLayer / TUnHideAll | command，无坐标或选择参数 |
| TLockLayer / TUnLockLayer / TLockOtherLayer | command、selection_handles |
| TMkHide / TLocVis / TUnHide | command、selection_handles |
| TRevPline | command、selection_handles；目前为开放原生多段线，检查顶点及弧段方向 |
| TRemoveDup | command、selection_handles；目前为精确重复的原生直线、圆，保留三维圆的法向证据 |
| TModElev | command、selection_handles；原生命令未产生效果，改用已验证的ActiveX投影，原生直线、XY平行圆/弧、轻量多段线及天正墙/柱；三维多段线和倾斜圆弧拒绝 |
| TChHeight | command、selection_handles、parameters: {height?, elevation?}，至少一项；天正墙及标准柱 |
| TWallThick | command、selection_handles、parameters: {width}；按基线两侧居中分配 |
| TChWallTerm | command、selection_handles、parameters: {closed: true/false} |
| TGChangeLayer | command、selection_handles、parameters: {layer}；目标层不存在时创建 |
| TPelChangeColor | command、selection_handles、parameters: {color: 0..256}；图块内部颜色处理待单独适配 |

属性适配直接调用已安装类型库中确认的ActiveX属性，返回 command_invoked:false 和实际执行通道，不声称调用了天正交互命令。写入前读完原值；异常后尝试逐项恢复，并核对恢复是否完成，仍非数据库原子事务。

api_info 是只读接口，data为 {selection_handles:[最多4个句柄], library_type_indices?:[最多8个类型索引]}。它读取接口名称、属性/方法文档和已安装类型库元数据，不执行客户端指定的方法。

### 已禁用的直接工厂路线

空的 AddCustomObject 柱对象在本次环境触发 tch_kernal.arx 访问冲突（0xc0000005）。曾补充的3项ObjectDBX工厂映射全部回滚，临时零尺寸柱移除，factory_probe 在连接CAD前直接返回 E_TIANZHENG_FACTORY_DISABLED。不要恢复或重复此路线。

检查器会在零尺寸柱/矩形上标记 uninitialized_custom_geometry，跳过已知危险的形状属性读取；修改与样板创建会拒绝这种对象。

### 初始化样板创建

通过已有的、正确初始化的天正对象Copy后修改参数。TGColumn的轴线对齐矩形柱已通过实机验证，目录标记自动配方可用。TRect也已通过尺寸、厚度、标高及样板保持不变的实机验证。TRect表示绘制天正矩形对象，TDelWallShape才是矩形立面，二者不能混用。

- TGColumn: data {command, template_handle, points, parameters:{width, depth, height}}；样板须为轴线对齐、非零尺寸的矩形柱。
- 同一矩形柱样板可使用 parameters:{shape:"circle",diameter,height}，或 {shape:"hexagon"|"octagon",side_length,height}。这三种截面的目标转角固定为0°，按360°周期校验天正 Rotation 读回；不接受其他旋转、源截面或任意异形参数。实测圆柱直径750、高3450、两个位置与标高175/320；正六/八边形边长420、高3350、标高210，均核对尺寸、截面及未选对象不变。这些是 TGColumn 的新增限定模式，不另增已通过命令数量。
- TRect: data {command, template_handle, points, parameters:{length, width, height?}}；样板须为轴线对齐、非零尺寸的天正矩形。

points代表几何中心的XY坐标及语义标高Z；支持1至32个位置。天正柱在平面模式下包围框Z仍为0，标高必须读取Elevation属性验证；高度读取Height，不用平面框高度判断。实现检查尺寸、位置、标高、类名和样板不变，失败尝试移除新建副本。首次正确样板需经支持的天正命令创建并验证，不从未初始化对象获得。
配方非原子操作；失败以 rollback_complete 和 cleanup_errors 判断补偿结果。超时或不确定写入先只读检查，不重发。正六边形曾因接近360°的零角读回而校验失败，失败副本已完整删除；修正等价角判定后实测通过，并未移除转角或几何校验。

## 位移配方与图层候选（2026-10-14）

TMove、TDragMove、TDragCopy的execute数据为 {command, selection_handles, parameters:{offset:[dx,dy,dz]}}，二维offset补Z=0。当前支持世界坐标平移；自由移动/复制中的旋转、镜像及改基点模式仍未适配。对象范围为原生直线、圆、圆弧、轻量多段线，以及初始化的天正墙、柱、矩形；门窗及图块不属于当前范围。返回command_invoked:false，实际调用ActiveX Move/Copy。

位移核对包围框、语义标高、尺寸、对象数量及未选择对象不变。柱和矩形平面框的Z保持0；墙的包围框随标高变化。失败尝试补偿位移或移除副本，核对rollback_complete，仍非原子事务。

TOffLayer、TOffOtherLayer、TOnLayer、TFreezeLayer、TFreezeOtherLayer、TUnFreezeLayer已实现句柄选择对应图层的属性适配，data为 {command, selection_handles}，不接收points。通过固定的LayerOn/Freeze属性实现；冻结当前图层会在写入前拒绝。这6项尚未完成实机验收，execution_contract.recipe_verified_live=false，candidate_verification=pending_live_layer_state_test。首次属性写入被CAD拒绝，已确认rollback_complete=true；不计为功能通过。

execution_contract.automated_recipe_available表示实现已存在；recipe_verified_live单独区分当前配方是否经过实机验收。某一有限模式通过不代表覆盖该命令的全部能力。
