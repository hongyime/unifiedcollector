# 动态延迟配置

这个文件包含了用于减少Strava API 429速率限制错误的动态延迟配置。

## 配置说明

**重要提示**: 
- 最小延迟为 **5秒**，用于避免429速率限制错误
- 速率限制重试的最大退避延迟为 **300秒（5分钟）**
- 所有延迟配置范围都基于5秒的最小值

所有的延迟都是随机范围，以避免规律的请求模式。每个API调用都会在配置的范围内选择一个随机延迟时间。

## 环境变量

### 基础API延迟
- `API_DELAY_MIN_SECONDS` - 最小API调用延迟（默认: 5.0秒）
- `API_DELAY_MAX_SECONDS` - 最大API调用延迟（默认: 10.0秒）

### Feed延迟（动态订阅动态）
- `FEED_DELAY_MIN_SECONDS` - Feed页面最小延迟（默认: 5.0秒）
- `FEED_DELAY_MAX_SECONDS` - Feed页面最大延迟（默认: 12.0秒）

### Backfill延迟（历史数据回填）
- `BACKFILL_DELAY_MIN_SECONDS` - Backfill最小延迟（默认: 5.0秒）
- `BACKFILL_DELAY_MAX_SECONDS` - Backfill最大延迟（默认: 15.0秒）

### Stream延迟（轨迹流数据）
- `STREAM_DELAY_MIN_SECONDS` - Stream数据最小延迟（默认: 5.0秒）
- `STREAM_DELAY_MAX_SECONDS` - Stream数据最大延迟（默认: 8.0秒）

### Roster延迟（关注者列表）
- `ROSTER_DELAY_MIN_SECONDS` - Roster最小延迟（默认: 5.0秒）
- `ROSTER_DELAY_MAX_SECONDS` - Roster最大延迟（默认: 10.0秒）

### 调试选项
- `DEBUG_DELAYS` - 启用延迟调试输出（默认: false）

## 配置示例

### 保守配置（减少429错误）
```bash
# 增加所有延迟范围
API_DELAY_MIN_SECONDS=8.0
API_DELAY_MAX_SECONDS=15.0
FEED_DELAY_MIN_SECONDS=10.0
FEED_DELAY_MAX_SECONDS=18.0
BACKFILL_DELAY_MIN_SECONDS=10.0
BACKFILL_DELAY_MAX_SECONDS=20.0
STREAM_DELAY_MIN_SECONDS=8.0
STREAM_DELAY_MAX_SECONDS=12.0
ROSTER_DELAY_MIN_SECONDS=8.0
ROSTER_DELAY_MAX_SECONDS=15.0
```

### 平衡配置（推荐）
```bash
# 使用默认值，适用于大多数情况
API_DELAY_MIN_SECONDS=5.0
API_DELAY_MAX_SECONDS=10.0
FEED_DELAY_MIN_SECONDS=5.0
FEED_DELAY_MAX_SECONDS=12.0
BACKFILL_DELAY_MIN_SECONDS=5.0
BACKFILL_DELAY_MAX_SECONDS=15.0
STREAM_DELAY_MIN_SECONDS=5.0
STREAM_DELAY_MAX_SECONDS=8.0
ROSTER_DELAY_MIN_SECONDS=5.0
ROSTER_DELAY_MAX_SECONDS=10.0
```

### 激进配置（更快的速度，可能增加429错误）
```bash
# 最小延迟必须至少5秒以避免429
API_DELAY_MIN_SECONDS=5.0
API_DELAY_MAX_SECONDS=7.0
FEED_DELAY_MIN_SECONDS=5.0
FEED_DELAY_MAX_SECONDS=8.0
BACKFILL_DELAY_MIN_SECONDS=5.0
BACKFILL_DELAY_MAX_SECONDS=10.0
STREAM_DELAY_MIN_SECONDS=5.0
STREAM_DELAY_MAX_SECONDS=6.0
ROSTER_DELAY_MIN_SECONDS=5.0
ROSTER_DELAY_MAX_SECONDS=7.0
```

### 调试配置
```bash
# 启用延迟调试输出
DEBUG_DELAYS=true

# 其他延迟配置保持平衡
API_DELAY_MIN_SECONDS=5.0
API_DELAY_MAX_SECONDS=10.0
```

## 如何配置

### 方法1: 环境变量
在运行应用时设置环境变量：

**Windows (PowerShell):**
```powershell
$env:API_DELAY_MIN_SECONDS="5.0"
$env:API_DELAY_MAX_SECONDS="10.0"
py -m ingestion.main
```

**Windows (CMD):**
```cmd
set API_DELAY_MIN_SECONDS=5.0
set API_DELAY_MAX_SECONDS=10.0
py -m ingestion.main
```

**Linux/macOS:**
```bash
export API_DELAY_MIN_SECONDS=5.0
export API_DELAY_MAX_SECONDS=10.0
python -m ingestion.main
```

### 方法2: .env 文件
在你的项目根目录创建 `.env` 文件：

```
API_DELAY_MIN_SECONDS=5.0
API_DELAY_MAX_SECONDS=10.0
FEED_DELAY_MIN_SECONDS=5.0
FEED_DELAY_MAX_SECONDS=12.0
DEBUG_DELAYS=true
```

## 监控和调优

### 启用延迟调试
设置 `DEBUG_DELAYS=true` 来查看延迟日志：

```
[delay] Sleeping for 2.34s (base: 2.34s)
[backoff] Attempt 1: calculated delay 30.00s (exp: 30.00s, base: 30.00s, max: 300.00s)
```

### 监控429错误
如果仍然遇到429错误，建议：
1. 增加延迟范围
2. 减少 `BACKFILL_PARALLELISM` 并发数
3. 启用 `DEBUG_DELAYS` 查看实际延迟时间

### 性能平衡
找到合适的延迟配置来平衡：
- 稳定性（减少429错误）
- 速度（完成时间）

## 指数退避策略

当遇到429错误时，系统会使用指数退避策略：
- 第一次重试: 30秒
- 第二次重试: 60秒  
- 第三次重试: 120秒
- 最大延迟: 300秒（5分钟）

这个过程会自动添加额外的随机抖动来避免多个客户端同步重试。

## 注意事项

1. **延迟是累积的**: 每个API调用都会在配置的延迟基础上添加随机时间
2. **重试会增加更多延迟**: 429错误会触发额外的延迟
3. **并发考虑**: 如果使用并发（`BACKFILL_PARALLELISM > 1`），延迟会在每个线程中独立工作
4. **不同账号可能有不同的限制**: 根据你的Strava账号状态调整配置

## 故障排除

### 仍然频繁遇到429错误
尝试以下步骤：
1. 增加所有延迟范围50%
2. 减少并发数到1
3. 添加更长的延迟范围
4. 在非高峰时段运行

### 速度太慢
可以尝试：
1. 适当减少延迟范围
2. 减少随机抖动范围
3. 监控429错误率，找到最佳平衡点

### 调试信息不显示
确保 `DEBUG_DELAYS=true` 已正确设置，并且环境变量已被正确加载。