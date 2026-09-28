# 抖音 SSR 抓取失效 — 诊断记录（2026-09-28）

## 症状
`analyze_video.py` 报 `SSR 解析失败: videoInfoRes.item_list 为空`。

## 根因（已确诊）
抖音**分享页不再下发视频数据**。旧解析路径失效，非本代码 bug。

## 实测证据（服务器 210.45.73.166 上复现）

| 入口 | 结果 |
|------|------|
| 短链 `v.douyin.com/xxx` | 302 正常，可拿到 video_id |
| `iesdouyin.com/share/video/<id>` | 200，32KB HTML，`_ROUTER_DATA` 存在但**只有 `video_layout` + `video_(id)/page`**，后者仅含 `itemId`/`serverToken`/`abParams`/`ua` 等页面配置 |
| `window._SSR_DATA` | 存在但 **`data: {}` 空** |
| `item_list` / `videoInfoRes` / `play_addr` / `aweme_id` | HTML 中出现 **0 次** |
| `www.douyin.com/video/<id>` | 72KB 但纯 SPA 壳，0 个数据标记，仅 2 个 script 标签 |
| `m.douyin.com/share/video/<id>` | 32KB，同 `_ROUTER_DATA`+`_SSR_DATA` 空结构 |
| 移动端 UA | 无差别，同样无数据 |

## API 路径状态
- `www.douyin.com/aweme/v1/web/aweme/detail/` → **`Blocked by ArgusSecurityPlugin Uifid Not Found`**
- 带 `ttwid` cookie（可从 share 页或 `ttwid.bytedance.com/ttwid/union/register/` 拿到）→ **仍报 Uifid Not Found**
- 带随机伪造 UIFID → 同样被拒（Uifid 必须是 JS SDK 真实计算的设备指纹）
- `www.douyin.com/` 首页只下发 `__ac_nonce`（验证码前置）

## 结论
视频元数据现在只能通过需签名的客户端 API 获取，签名链涉及：
1. **ttwid** — 可获取 ✅（curl 直接拿）
2. **Uifid** — 需 JS 计算 ❌（难点）
3. **a_bogus / X-Bogus** 签名 — 需 JS 计算（未验证）

## 约束（项目 AGENTS.md）
不登录、不用 Browserbase/CDP、不绕验证码、不绕隐私限制。
