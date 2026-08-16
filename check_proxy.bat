@echo off
rem 代理节点 geoblock 检测：走 127.0.0.1:10808 探测 Polymarket 下单端点。
rem 返回 400/401（参数类错误）= 节点放行，可实盘；返回 403 Trading restricted = 节点被封锁。
cd /d %~dp0
set HTTPS_PROXY=http://127.0.0.1:10808
set HTTP_PROXY=http://127.0.0.1:10808
echo 正在检测 127.0.0.1:10808 出口节点是否被 Polymarket 放行...
echo.
curl -s -m 15 -X POST -H "Content-Type: application/json" -d "{}" "https://clob.polymarket.com/order"
echo.
echo.
echo [提示] 出现 Trading restricted = 被封锁，请换节点后再试；
echo        出现 error/success 字段（非 restricted）= 已放行，可启动实盘。
pause
