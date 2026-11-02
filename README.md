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

### Linux

Python 3.10+.

```bash
./setup_linux.sh
./CL-Stats-Desktop/запуск.sh
```

`./CL-Stats-Desktop/запуск-фон.sh` keeps the server running without a window.

### Windows

Python 3.10+ on PATH, once:

```bat
setup_windows.bat
CL-Stats-Desktop\запуск.bat
```

A windowed app, built on Windows:

```bat
CL-Stats-Desktop\tools\build_win.bat
```

Take the whole folder `dist\WoTB Stats by Clark\`, not the exe alone. `data` and `chrome_profile` appear beside the exe on the first launch. Do not give those two folders to someone else. Run `setup_windows.bat` only the first time.

The exe is not code-signed. SmartScreen may say Windows protected your PC: **More info → Run anyway**. Defender sometimes flags a PyInstaller build. That is a false positive of this kind of build. Add the folder to exclusions if you built it yourself.

### macOS

The Mac app can only be built on a Mac. Python 3.10+, and Chrome, Chromium, Edge or Brave.

```bash
chmod +x CL-Stats-Desktop/tools/build_mac.sh CL-Stats-Desktop/запуск.command
cd CL-Stats-Desktop
./tools/build_mac.sh
```

The result is `dist/WoTB Stats by Clark.app`. Gatekeeper will warn that the developer cannot be verified. Right-click the app → **Open**. If macOS says the app is damaged after a download:

```bash
xattr -dr com.apple.quarantine "/path/to/WoTB Stats by Clark.app"
```

Without a built app, double-click `CL-Stats-Desktop/запуск.command`. Details are in `CL-Stats-Desktop/README_MAC.txt`.

### Your data

Export a zip from the top bar before you move to another computer. Do not copy a Linux `data` folder onto Windows or a Mac: game paths inside it belong to that system. JSON from the old web app is added in Settings → **Add from JSON**. It does not replace the zip.

Ready-made builds for Windows, macOS and Linux will be attached to [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases) after they are packed and checked. This repository is the source.

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

### Linux

Python 3.10+.

```bash
./setup_linux.sh
./CL-Stats-Desktop/запуск.sh
```

`./CL-Stats-Desktop/запуск-фон.sh` лишає сервер без вікна.

### Windows

Один раз, якщо Python 3.10+ є в PATH:

```bat
setup_windows.bat
CL-Stats-Desktop\запуск.bat
```

Віконний застосунок збирається на самому Windows:

```bat
CL-Stats-Desktop\tools\build_win.bat
```

Віддавай усю теку `dist\WoTB Stats by Clark\`, не один exe. `data` і `chrome_profile` з’являться біля exe після першого запуску. Ці дві теки іншим не віддавай. `setup_windows.bat` запускай лише перший раз.

Exe без підпису. SmartScreen може написати, що Windows захистив ПК: **Додаткові відомості → Усе одно запустити**. Defender інколи лається на збірку PyInstaller. Для власної збірки це хибне спрацювання. Теку можна додати у винятки.

### macOS

Збірку `.app` можна зробити лише на Mac. Python 3.10+ і Chrome, Chromium, Edge або Brave.

```bash
chmod +x CL-Stats-Desktop/tools/build_mac.sh CL-Stats-Desktop/запуск.command
cd CL-Stats-Desktop
./tools/build_mac.sh
```

На виході `dist/WoTB Stats by Clark.app`. Gatekeeper напише, що розробника не перевірено. Правий клік по застосунку → **Відкрити**. Якщо після завантаження macOS каже, що файл пошкоджено:

```bash
xattr -dr com.apple.quarantine "/шлях/WoTB Stats by Clark.app"
```

Без зібраного застосунку — подвійний клік по `CL-Stats-Desktop/запуск.command`. Деталі в `CL-Stats-Desktop/README_MAC.txt`.

### Твої дані

Перед переносом на інший комп’ютер зроби zip зверху. Теку `data` з Linux не копіюй на Windows чи Mac: шляхи гри там від тієї системи. JSON зі старого веба додається в налаштуваннях → **Додати з JSON**. Zip він не замінює.

Готові збірки для Windows, macOS і Linux з’являться в [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases), коли їх запакують і перевірять. Цей репозиторій — вихідний код.

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

### Linux

Python 3.10+.

```bash
./setup_linux.sh
./CL-Stats-Desktop/запуск.sh
```

`./CL-Stats-Desktop/запуск-фон.sh` оставляет сервер без окна.

### Windows

Один раз, если Python 3.10+ есть в PATH:

```bat
setup_windows.bat
CL-Stats-Desktop\запуск.bat
```

Оконное приложение собирается на самом Windows:

```bat
CL-Stats-Desktop\tools\build_win.bat
```

Отдавай всю папку `dist\WoTB Stats by Clark\`, не один exe. `data` и `chrome_profile` появятся рядом с exe после первого запуска. Эти две папки другим не отдавай. `setup_windows.bat` запускай только в первый раз.

Exe без подписи. SmartScreen может написать, что Windows защитил ПК: **Подробнее → Всё равно выполнить**. Defender иногда ругается на сборку PyInstaller. Для своей сборки это ложное срабатывание. Папку можно добавить в исключения.

### macOS

Сборку `.app` можно сделать только на Mac. Python 3.10+ и Chrome, Chromium, Edge или Brave.

```bash
chmod +x CL-Stats-Desktop/tools/build_mac.sh CL-Stats-Desktop/запуск.command
cd CL-Stats-Desktop
./tools/build_mac.sh
```

На выходе `dist/WoTB Stats by Clark.app`. Gatekeeper напишет, что разработчик не проверен. Правый клик по приложению → **Открыть**. Если после загрузки macOS говорит, что файл повреждён:

```bash
xattr -dr com.apple.quarantine "/путь/WoTB Stats by Clark.app"
```

Без собранного приложения — двойной клик по `CL-Stats-Desktop/запуск.command`. Подробности в `CL-Stats-Desktop/README_MAC.txt`.

### Твои данные

Перед переносом на другой компьютер сделай zip сверху. Папку `data` с Linux не копируй на Windows или Mac: пути игры там от той системы. JSON со старого веба добавляется в настройках → **Добавить из JSON**. Zip он не заменяет.

Готовые сборки для Windows, macOS и Linux появятся в [Releases](https://github.com/clark321299/WoTB-Stats-by-Clark/releases), когда их упакуют и проверят. Этот репозиторий — исходный код.
