# custom-exchange

个人自用分支，在 [freqtrade](https://github.com/freqtrade/freqtrade) 的 `develop` 上增加 Aster 和 MEXC 合约。不是官方支持的交易所，也没有写进 `SUPPORTED_EXCHANGES`。

官方说明仍以根目录的 `README.md` 为准。本文件是额外加上的，不改官方 README，方便以后直接合并上游。

## 相对上游多出来的内容

两个交易所都只做了现货和 USDT 本位逐仓永续。全仓没有做。

| 交易所 | 配置名 | 认证 | 实盘启动时 |
| --- | --- | --- | --- |
| Aster | `aster` | `privateKey`（`0x` 加 64 位十六进制）。钱包地址由私钥推导 | 空仓时把账户改成单向持仓 |
| MEXC | `mexc` | `key` / `secret` | 空仓时把账户改成单向持仓 |

更细的限制写在本分支的 `docs/exchanges.md` 里，章节是 Aster 和 MEXC。那里会讲：

- Aster 用杠杆档位算最大杠杆和强平价。没有私钥的干跑、回测退回到 `exchangeInfo` 上的保守值。实测账户费率是挂单 0、吃单 0.04%，干跑请在配置里写 `"fee": 0.0004`。
- MEXC 的计划止损单最多保留 7 天。合约 K 线和订单簿走 WebSocket；现货 WebSocket 关掉了。本机访问不了 `api.mexc.com` 时，设置 `https_proxy`，子类会转给 CCXT。

## 镜像

`linux/amd64`，已安装 FreqUI。

```text
sitdownkevin/freqtrade:custom-exchange
```

同一次构建还有按提交打的标签 `sitdownkevin/freqtrade:6ead7675`。

## 从上游更新

```bash
git fetch upstream
git merge upstream/develop
```

`upstream` 是 `https://github.com/freqtrade/freqtrade.git`。本地和 `origin` 的 `develop` 跟踪这条上游分支，不要往 `develop` 上提交交易所改动。

本文件是新文件，合并上游时不会和官方 `README.md` 冲突。适配代码改过下面这些上游已有文件，上游如果也改了同一处，冲突会出在这里：

- `docs/exchanges.md`
- `freqtrade/exchange/__init__.py`
- `tests/exchange/test_exchange.py`
- `tests/exchange_online/conftest.py`
- `tests/rpc/test_rpc_apiserver.py`

只属于本分支的新文件是 `freqtrade/exchange/aster.py`、`freqtrade/exchange/mexc.py`，以及对应的 `tests/exchange/test_aster.py`、`tests/exchange/test_mexc.py`、`tests/exchange_online/test_mexc_live_orders.py`。
