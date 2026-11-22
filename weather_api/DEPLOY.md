# 部署到服务器

## 一、上传两个目录

```
weather_api\        代码
db_mirror\          数据（392 MB）
```

两个目录放**同一个父目录**下，相对位置不变。例如放到 `/opt/weather/`：

```
/opt/weather/weather_api/
/opt/weather/db_mirror/
```

## 二、装依赖

```bash
cd /opt/weather
python3 -m venv venv
source venv/bin/activate
pip install -r weather_api/requirements.txt
```

## 三、启动

```bash
cd /opt/weather
python -m weather_api.main --port 8900
```

默认监听 `0.0.0.0:8900`，服务器外部可直接访问。
必须在 `weather_api` 的**父目录**下执行（`-m weather_api.main` 要求）。

验证：

```bash
curl http://服务器IP:8900/api/health
```

## 四、常驻运行

### Linux（systemd）

`/etc/systemd/system/weather-api.service`：

```ini
[Unit]
Description=Weather API
After=network.target

[Service]
WorkingDirectory=/opt/weather
Environment="PYTHONIOENCODING=utf-8"
ExecStart=/opt/weather/venv/bin/python -m weather_api.main --port 8900
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now weather-api
```

### Windows（NSSM）

```powershell
nssm install WeatherAPI "D:\Android PDF\venv\Scripts\python.exe" "-m weather_api.main --port 8900"
nssm set WeatherAPI AppDirectory "D:\Android PDF"
nssm start WeatherAPI
```

## 五、放行端口

```bash
# Linux 防火墙
sudo firewall-cmd --add-port=8900/tcp --permanent && sudo firewall-cmd --reload
```

**云服务器还要在控制台的安全组里放行 8900 端口**，这一步最常漏。

## 六、数据放别处（可选）

```bash
export WEATHER_DB_ROOT=/data/db_mirror
```

不设就是 `<weather_api 父目录>/db_mirror`。

## 七、需要 HTTPS 时

前面加一层 Nginx：

```nginx
server {
    listen 443 ssl;
    server_name api.your-domain.com;
    ssl_certificate     /etc/ssl/your.crt;
    ssl_certificate_key /etc/ssl/your.key;

    location / {
        proxy_pass http://127.0.0.1:8900;
        proxy_read_timeout 600s;   # 导出/批量补齐较慢
    }
}
```

## 八、调用方式

前端（CORS 已全开，可直接跨域调用）：

```js
fetch("http://服务器IP:8900/api/overview")
  .then(r => r.json())
  .then(res => console.log(res.data));
```

返回格式统一：

```json
{ "ok": true, "data": ... }
{ "ok": false, "error": "..." }
```

日期直接用真实历史年份（1951–2025），源库的 +100 年偏移已自动修正。

## 九、常用接口

| 接口 | 用途 |
|---|---|
| `GET /api/overview` | 数据集概览 |
| `GET /api/stations` | 站点列表（`province` / `search` / 分页） |
| `GET /api/weather/daily` | 某站某天/某段日值 |
| `GET /api/weather/day_rank` | 某天全国排行 |
| `GET /api/records/station` | 某站历史极值 |
| `GET /api/records/global` | 全国极值排行 |
| `GET /api/trend` | 时间趋势（日/月/年） |
| `GET /api/climate/monthly` | 常年逐月气候值 |
| `GET /api/report/table` | 气象填报表（12 月值） |
| `GET /api/report/export` | 填报表导出 Word/Excel |
| `GET /api/repair/scan` | 扫描缺测/异常 |
| `GET /api/repair/daily` | 修复坏值 |
| `GET /api/repair/new_batch` | 新录入批次自动补齐 |
| `GET /api/health` | 健康检查（探活） |

完整 32 个接口与参数：启动后访问 **`http://服务器IP:8900/docs`**。

## 十、注意

- **无鉴权**：任何人能访问端口就能调接口。对外公开前建议加 API Key 或走内网。
- **只读**：所有接口都不写数据库，`repair` 只返回修复建议。
- `exports/` 目录需可写（导出 Word/Excel）。
- `/api/repair/new_batch` 要跑十几秒，别卡在页面请求里，放定时任务或异步调。
