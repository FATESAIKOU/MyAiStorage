# Review：ime-kitty-keyboard（issue #23），實作之前

審的是 `openspec/changes/ime-kitty-keyboard` 的 proposal、spec、tasks（f032658）。
對照了 `src/agora/cli.py` 和 Textual 8.2.8 的原始碼：`textual/constants.py`、`textual/drivers/linux_driver.py`。
用一個子程序實際試了一次，見下面「實測」。沒有跑整合測試。

## 結論

**做法正確，時機也趕得上。有 1 個 High：常數名稱寫錯。** 改掉之後就可以開工。

## High

### K1 常數叫 `DISABLE_KITTY_KEY`，不是 `DISABLE_KITTY`

- `textual/constants.py:116` 是 `DISABLE_KITTY_KEY: Final[bool] = _get_environ_bool("TEXTUAL_DISABLE_KITTY_KEY")`。
- 驅動程式讀的也是這個名字：`linux_driver.py:285` `if not constants.DISABLE_KITTY_KEY:`。
- proposal、spec 的 Scenario 和 tasks 1.2 都寫成 `textual.constants.DISABLE_KITTY`。照這樣寫測試，會得到 `AttributeError`，而不是預期的「為假」。
- 三處都要改成 `DISABLE_KITTY_KEY`。

## 時機：在 `from agora import tui` 之前 setdefault，趕得上

- **Textual 什麼時候讀這個值：**
  - `textual.constants` 在**第一次被 import 時**讀 `os.environ`，結果存成模組常數，之後不會再讀；
  - 驅動程式在 `start_application_mode` 時才看這個常數，決定要不要送出 `\x1b[>…u`。
  - 所以只要在 Textual 第一次被 import 之前設好環境變數就行。
- **agora 裡誰會 import Textual：**
  - `src/agora` 底下只有 `tui.py` import Textual；
  - `cli.py` 最上面 import 的 `header`、`store`、`agents.base`、`jsonschema`、`yaml` 都不會帶進 Textual；
  - 實測：在乾淨的環境裡 `import agora.cli` 之後，`"textual" in sys.modules` 是 False。
- **怎麼放：** `cli.main` 的互動模式分支（`cli.py:1025`）在 `from agora import tui` 前一行做 `os.environ.setdefault(...)`。
- **會壞掉的情況：** 以後有人在 `cli.py` 最上面，或是在它 import 的模組裡，import 了 Textual（或 `agora.tui`），這行就會悄悄失效，而且畫面上看不出來。所以測試要把「`import agora.cli` 不會帶進 Textual」也當成一個檢查（見 K2）。

## 測試方式可行，但要照下面的寫法（K2，Medium）

tasks 1.2 寫「import 互動模式之後」，但互動模式要 TTY，而且會真的把 App 跑起來。
直接 import `agora.tui` 也不行：那樣測到的只是 import 的順序，不是 `cli.main` 有沒有先設好。

可行的做法（已在 scratchpad 試過）：在子程序裡，用一個假的 `agora.tui` 頂替真的。假的 `main()` 自己去 import `textual.constants`，這時才是 Textual 第一次被 import。

```python
import os, sys, types
import agora, agora.cli as cli
assert "textual" not in sys.modules            # 時機的前提：cli 本身不能帶進 Textual
fake = types.ModuleType("agora.tui")
def fake_main(paths):
    import textual.constants as c              # Textual 第一次被 import 是在這裡
    print(int(c.DISABLE_KITTY_KEY))
    return 0
fake.main = fake_main
sys.modules["agora.tui"] = fake; agora.tui = fake
sys.stdin.isatty = sys.stdout.isatty = lambda: True
sys.exit(cli.main([]))
```

- 子程序用 `env -i` 起，帶上 PATH 和隔離用的 `HOME`、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`、`AGORA_FOLDER_NAME=agora-test`、`AGORA_RCLONE`，都指到 `tmp_path`。
- `cli.main([])` 在互動分支裡只會呼叫 `store.Paths.from_env()` 和假的 `tui.main`，不會碰到任何資料。
- 要測三種情況：
  1. **沒有設定**：印出 `1`。
     - 現在的程式（還沒實作）印出的是 `0`，所以這個測試確實會先紅。
     - 這一點在 scratchpad 實際跑過：輸出是 `env: None DISABLE_KITTY_KEY: False`。
  2. **`TEXTUAL_DISABLE_KITTY_KEY=0`**：印出 `0`，而且子程序裡的環境變數仍然是 `"0"`。
  3. **指令模式**：子程序跑 `cli.main(["search", "x"])`。型態不是 `session`，會在 parse 之後馬上回傳 `EXIT_INPUT`，不碰任何資料。之後檢查 `"TEXTUAL_DISABLE_KITTY_KEY" not in os.environ`。
- 一定要用子程序：pytest 那個 process 早就因為 `test_tui.py` import 過 Textual 了，在同一個 process 裡測不出時機。

## Low

- **K3 只有 `"1"` 算關掉。**
  - `_get_environ_bool` 的判斷是 `== "1"`。
  - 所以使用者設成 `true`、`yes`，或設了但值是空字串（`TEXTUAL_DISABLE_KITTY_KEY=`）時，`setdefault` 不會蓋掉它，kitty 協定**會打開**。
  - 這符合 spec 說的「照使用者的設定」，但 README 那一句要寫成：「只有 `1` 會關掉；設成其他任何值（包括空字串）都會打開」，不要只寫「想打開就設 `0`」。
- **K4 環境變數會傳給子程序。**
  - `os.environ` 設了以後，互動模式開的子程序都會帶著它：每個動作跑的 `agora <動作>`、接續時開的 opencode／claude。
  - 這些都不是 Textual 程式，不會受影響；就算以後有 Textual 程式，關掉 kitty 也只是回到舊的按鍵編碼。不用改，在 design 或程式註解裡記一句就好。
- **K5 Windows 不適用。** 只有 `linux_driver`（macOS 也是走這個）會送 kitty 的序列，Windows 驅動沒有這段程式。agora 本來就只支援 macOS／Linux，不影響。

## 關掉 kitty 協定之後，按鍵還分得出來嗎（也回答 preview-search-keys 的第 (3) 點）

不用 kitty 協定時，終端機送的是傳統的編碼：

| 鍵 | 終端機送出的序列 | 和別的鍵混淆嗎 |
|---|---|---|
| `Tab` | `\t` | 和 `ctrl+i` 送出的一樣（傳統編碼本來就分不出來）。agora 沒有用 `ctrl+i`，沒問題 |
| `shift+tab` | `\x1b[Z` | 分得出來 |
| `[`、`]` | 字元本身 | 分得出來 |
| `ctrl+t` | `\x14` | 分得出來 |
| `ctrl+q` | `\x11` | 分得出來（Textual 在 raw mode 會關掉 IXON） |
| `G`、`N`、`P` | 大寫字元 | 分得出來 |
| `Esc` | `\x1b`，等 `ESCDELAY` 100 ms 才確定是 Esc | 見下面 |

- 唯一的邊緣情況：按 `Esc` 之後 100 ms 內又按了 `[`，兩個鍵會被當成 `\x1b[`（CSI 序列的開頭），Esc 和 `[` 可能都收不到。
  - 例如在預覽區按 Esc 清掉標亮，再很快切回清單按 `[` 換頁。
  - 實際上中間至少要按一次 Tab，很難在 100 ms 內做完，列為 Low 即可。
- 傳統編碼分不出 `shift+enter`、`ctrl+enter`、`ctrl+shift+字母` 這類組合，但兩個 change 都沒有用到。
- 結論：proposal 說 `Tab`、`shift+tab`、`[`、`]`、`ctrl+t`、`ctrl+q` 都送得出來，是對的。

## tasks

- 1.1 到 1.3 都只動 `cli.py`、一個新的測試檔和 README，和 `preview-search-keys` 要改的 `tui.py` 沒有交集。
- README 兩個 change 都要改：這邊加一句，那邊改按鍵表。是不同段落，但合 PR 時要注意衝突。
- 1.2 要照 K2 的寫法；常數名稱照 K1 改。

---

## 程式審查（a85bb80）

審的是 a85bb80 的 `src/agora/cli.py`、`tests/unit/test_ime_kitty.py`、README 與 tasks，對照的是 change 的 spec、tasks，以及上面的 K1～K5。

**結論：可以合。沒有 High，也沒有 Medium。** 下面有 4 個 Low，改不改都不影響合併。

### 對照

- **K1 常數名稱**：測試讀的是 `textual.constants.DISABLE_KITTY_KEY`，tasks 1.2 也改過了。✓
- **時機**：`cli.py:1025` 在互動模式的分支裡，`from agora import tui` 的前一行做 `os.environ.setdefault(...)`。✓
- **K2 的子程序測試**：照建議用假的 `agora.tui`，三種情況都有測：
  - 沒有設定：`DISABLE_KITTY_KEY=1`，而且環境變數是 `1`；
  - 設成 `0`：常數是 `0`，環境變數仍然是 `0`，有照使用者的設定；
  - 指令模式：跑了 `search`（不給型態）和 `search session --no-sync` 兩次，之後環境變數都沒有被設。
  - 互動模式那兩個測試也會檢查「`import agora.cli` 不會帶進 Textual」。✓
- **K3 README**：寫明「只有 `1` 會關掉；設成其他任何值，包括空字串，都會打開」。✓

### 隔離：不會碰到真實目錄

- 測試檔最上面 import 了 `tests/_guard.py`，在 pytest 以外又沒有隔離時，import 這個檔就會被擋下。
- 子程序的環境是**從零開始組的**，不是從父程序複製來的，只放了：
  - `PATH`、`PYTHONPATH`；
  - `HOME`、`AGORA_CONFIG`、`AGORA_CACHE_DIR`、`AGORA_STATE_DIR`，都指到 `tmp_path`；
  - `AGORA_FOLDER_NAME=agora-test`；
  - `AGORA_RCLONE`，指到包著 `tests/fakes/fake_rclone.py` 的 wrapper。
- 所以就算父程序的環境沒有隔離，子程序也碰不到真實的目錄。指令模式那次實際執行的 `search session --no-sync` 也只會讀寫 `tmp_path`。
- 父程序只做了 `from agora import cli`（用來取 `EXIT_INPUT`），這沒有副作用。

### 測試抓得到錯嗎：在副本裡故意改壞四種

都在 `git archive a85bb80` 解開的副本裡改，跑的是 `test_ime_kitty.py`：

| 故意改壞成這樣 | 結果 |
|---|---|
| 拿掉 setdefault 那一行 | 1 failed（沒設定的那一個） |
| 改成 `os.environ[...] = "1"`，會蓋掉使用者的設定 | 1 failed（設成 0 的那一個） |
| setdefault 移到 `main()` 的最前面（指令模式也會設） | 1 failed（指令模式的那一個） |
| 在 `cli.py` 最上面 `import textual.constants` | 2 failed（「cli 不能帶進 Textual」的檢查） |

四種都抓得到。

### Low（不擋合併）

- **C1**：`test_ime_kitty.py` 裡的 `import pytest` 沒有用到。
- **C2**：`cli.py` 新加的註解是中文，但這個檔的其他註解都是英文。建議照周圍的寫法，例如 `# Textual's kitty keyboard protocol makes an IME's Enter erase the chosen word (issue #23)`。
- **C3：README 那句前後讀起來像矛盾。**
  - 前面說「預設關掉」，後面說「只有 `=1` 會關掉」，讀的人可能以為沒設定時是打開的。
  - 建議寫成：「沒有設定時，agora 會自己設成 `1`（關掉）；自己設了就照你的設定：只有 `1` 會關掉，其他任何值（包括空字串）都會打開。」
  - 另外，tasks 1.3 的字面還是「想打開就設 0」，和 README 的寫法不一樣，可以順手改成一致。
- **C4：指令模式的第二個呼叫比需要的多。**
  - `search session --no-sync` 真的跑了一次搜尋，還斷言 exit 0，這讓這個測試和搜尋的行為綁在一起：以後搜尋空索引時的 exit code 一改，這個測試就會紅。
  - 第一個呼叫（`["search"]`，parse 完馬上回傳）已經足以證明「指令模式不設」。建議拿掉第二個，或者只留「環境變數沒有被設」那個斷言，不要斷言 exit code。

### 還沒做的

- tasks 2.2（本人在 herdr 的 pane 裡用輸入法選字）。spec 的第三個 Scenario 只能用人工驗。

### 測試執行

- `uv run pytest tests/unit/test_ime_kitty.py`：3 passed。
- **完整 unit 是在 `git archive cc1891a` 的乾淨副本裡跑的：** compileall 通過，563 passed。
  - 這樣做是為了不把 impl3 還沒 commit 的 `tui.py`、`test_tui.py` 混進來。
  - 這個副本就是 HEAD，裡面包含 a85bb80。
  - 跑的時候設了 `PYTHONPATH` 指到副本的 `src`，所以載入的是副本的程式。
- 沒有跑整合測試。
