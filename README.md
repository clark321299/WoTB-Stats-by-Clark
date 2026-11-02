# WoTB Stats by Clark

Local stats for World of Tanks Blitz. The window stays on your computer. Nothing in this project is sent to a server of mine. Wargaming is contacted only from your machine, when you load an account or refresh tank names.

The app folder inside this repository is `CL-Stats-Desktop`. The `cl_platform` folder next to it is required. Your profiles and backups are created on your machine and are not part of this repository.

## English

### What it does

- Account stats, a tank list and a tank rating.
- Several profiles on one computer. A profile can have a PIN.
- Sessions and an archive.
- The Olenimeter tab: recent battles, a forecast, platoon notes.
- Replays and a battle map, when the game folder is found. Another replay folder can be set in Settings.
- Tank names update from the Wargaming encyclopedia and from the game’s own data, without unpacking the app.
- A zip export is a full copy: profiles, sessions and the Olenimeter.
- An older JSON file (the web app backup, or an old desktop backup) can be added from Settings. It adds sessions and the archive. Replays stay as they are. The same row is not added twice.
- The first launch on an empty computer shows a short welcome. A computer that already has data does not.

You need Chrome, Chromium, Edge or Brave. The page opens at `http://127.0.0.1:17321`. Closing the window stops the app.

### For a friend

A friend does not install Python and does not run `setup_windows.bat`.

The ready downloads will be in [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases), one file per system:

- **Windows** — a folder with `WoTB Stats by Clark.exe`. Unzip it and start the exe. The exe is not code-signed. If SmartScreen appears: **More info → Run anyway**.
- **macOS** — `WoTB Stats by Clark.app`. Right-click → **Open**. That file can only be built on a Mac, so it is not here yet.
- **Linux** — there is no separate exe. Once: `./setup_linux.sh`, then `./CL-Stats-Desktop/запуск.sh`.

Those release files are not attached yet. The Windows folder is already packed on a Windows PC. It goes up here after a check that the new exe opens. Do not give anyone the `data` or `chrome_profile` folders that appear next to the exe after the first launch.

### Build it yourself

Only for packing the app.

Windows, on Windows, Python 3.10+ the first time:

```bat
setup_windows.bat
CL-Stats-Desktop\tools\build_win.bat
```

Give away the whole folder `dist\WoTB Stats by Clark\`, not the exe alone.

macOS, on a Mac:

```bash
cd CL-Stats-Desktop
./tools/build_mac.sh
```

The result is `dist/WoTB Stats by Clark.app`. If macOS says the downloaded app is damaged: `xattr -dr com.apple.quarantine "/path/to/WoTB Stats by Clark.app"`. Details are in `CL-Stats-Desktop/README_MAC.txt`.

### Your data

Export a zip from the top bar before you move to another computer. Do not copy a Linux `data` folder onto Windows or a Mac: game paths inside it belong to that system. JSON from the old web app is added in Settings → **Add from JSON**. It does not replace the zip.

## Українська

Локальна статистика World of Tanks Blitz. Вікно живе на твоєму комп’ютері. На мій сервер нічого не йде. Wargaming викликається лише з твоєї машини, коли ти відкриваєш акаунт або оновлюєш назви танків.

Тека застосунку в цьому репозиторії — `CL-Stats-Desktop`. Поруч обов’язкова тека `cl_platform`. Профілі й бекапи з’являються в тебе на диску і в репозиторій не входять.

### Що вміє

- Статистика акаунта, список танків і рейтинг танків.
- Кілька профілів на одному комп’ютері. На профіль можна поставити PIN.
- Сесії й архів.
- Вкладка Оленіметр: свіжі бої, прогноз, позначки взводу.
- Реплеї і карта бою, якщо теку гри знайдено. Іншу теку реплеїв можна вказати в налаштуваннях.
- Назви танків оновлюються з енциклопедії Wargaming і з даних самої гри, без розпаковування застосунку.
- Zip зверху — повна копія: профілі, сесії й Оленіметр.
- Старий JSON (бекап веба або старого десктопу) додається з налаштувань. Він дописує сесії й архів. Реплеї не замінюються. Той самий рядок вдруге не додається.
- Перший запуск на порожньому комп’ютері показує коротке вітання. Там, де дані вже є, вітання немає.

Потрібен Chrome, Chromium, Edge або Brave. Сторінка відкривається на `http://127.0.0.1:17321`. Хрестик на вікні зупиняє застосунок.

### Для друга

Друг не ставить Python і не запускає `setup_windows.bat`.

Готові файли будуть у [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases), по одному на систему:

- **Windows** — тека з `WoTB Stats by Clark.exe`. Розпакував і запустив exe. Підпису немає. Якщо SmartScreen завадить: **Додаткові відомості → Усе одно запустити**.
- **macOS** — `WoTB Stats by Clark.app`. Правий клік → **Відкрити**. Цей файл збирається лише на Mac, тому його ще немає.
- **Linux** — окремого exe немає. Один раз `./setup_linux.sh`, далі `./CL-Stats-Desktop/запуск.sh`.

Цих файлів у Releases ще немає. Тека для Windows уже зібрана на Windows-комп’ютері. Сюди вона ляже після перевірки, що новий exe відкривається. Теки `data` і `chrome_profile`, які з’являються біля exe після першого запуску, іншим не віддавай.

### Зібрати самому

Лише для того, хто пакує застосунок.

Windows, на самому Windows, Python 3.10+ перший раз:

```bat
setup_windows.bat
CL-Stats-Desktop\tools\build_win.bat
```

Віддавай усю теку `dist\WoTB Stats by Clark\`, не один exe.

macOS, на Mac:

```bash
cd CL-Stats-Desktop
./tools/build_mac.sh
```

На виході `dist/WoTB Stats by Clark.app`. Якщо macOS каже, що завантажений файл пошкоджено: `xattr -dr com.apple.quarantine "/шлях/WoTB Stats by Clark.app"`. Деталі в `CL-Stats-Desktop/README_MAC.txt`.

### Твої дані

Перед переносом на інший комп’ютер зроби zip зверху. Теку `data` з Linux не копіюй на Windows чи Mac: шляхи гри там від тієї системи. JSON зі старого веба додається в налаштуваннях → **Додати з JSON**. Zip він не замінює.

## Русский

Локальная статистика World of Tanks Blitz. Окно живёт на твоём компьютере. На мой сервер ничего не уходит. Wargaming вызывается только с твоей машины, когда ты открываешь аккаунт или обновляешь названия танков.

Папка приложения в этом репозитории — `CL-Stats-Desktop`. Рядом обязательна папка `cl_platform`. Профили и бэкапы появляются у тебя на диске и в репозиторий не входят.

### Что умеет

- Статистика аккаунта, список танков и рейтинг танков.
- Несколько профилей на одном компьютере. На профиль можно поставить PIN.
- Сессии и архив.
- Вкладка Оленимэтр: свежие бои, прогноз, пометки взвода.
- Реплеи и карта боя, если папка игры найдена. Другую папку реплеев можно указать в настройках.
- Названия танков обновляются из энциклопедии Wargaming и из данных самой игры, без распаковки приложения.
- Zip сверху — полная копия: профили, сессии и Оленимэтр.
- Старый JSON (бэкап веба или старого десктопа) добавляется из настроек. Он дописывает сессии и архив. Реплеи не заменяются. Та же строка второй раз не добавляется.
- Первый запуск на пустом компьютере показывает короткое приветствие. Там, где данные уже есть, приветствия нет.

Нужен Chrome, Chromium, Edge или Brave. Страница открывается на `http://127.0.0.1:17321`. Крестик на окне останавливает приложение.

### Для друга

Друг не ставит Python и не запускает `setup_windows.bat`.

Готовые файлы будут в [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases), по одному на систему:

- **Windows** — папка с `WoTB Stats by Clark.exe`. Распаковал и запустил exe. Подписи нет. Если SmartScreen мешает: **Подробнее → Всё равно выполнить**.
- **macOS** — `WoTB Stats by Clark.app`. Правый клик → **Открыть**. Этот файл собирается только на Mac, поэтому его ещё нет.
- **Linux** — отдельного exe нет. Один раз `./setup_linux.sh`, дальше `./CL-Stats-Desktop/запуск.sh`.

Этих файлов в Releases ещё нет. Папка для Windows уже собрана на Windows-компьютере. Сюда она ляжет после проверки, что новый exe открывается. Папки `data` и `chrome_profile`, которые появляются рядом с exe после первого запуска, другим не отдавай.

### Собрать самому

Только для того, кто пакует приложение.

Windows, на самом Windows, Python 3.10+ в первый раз:

```bat
setup_windows.bat
CL-Stats-Desktop\tools\build_win.bat
```

Отдавай всю папку `dist\WoTB Stats by Clark\`, не один exe.

macOS, на Mac:

```bash
cd CL-Stats-Desktop
./tools/build_mac.sh
```

На выходе `dist/WoTB Stats by Clark.app`. Если macOS говорит, что скачанный файл повреждён: `xattr -dr com.apple.quarantine "/путь/WoTB Stats by Clark.app"`. Подробности в `CL-Stats-Desktop/README_MAC.txt`.

### Твои данные

Перед переносом на другой компьютер сделай zip сверху. Папку `data` с Linux не копируй на Windows или Mac: пути игры там от той системы. JSON со старого веба добавляется в настройках → **Добавить из JSON**. Zip он не заменяет.
