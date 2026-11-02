WoTB Stats by Clark — macOS 11+
================================

Збірка лише на Mac (PyInstaller не робить .app з Linux/Windows).
Потрібно: Python 3.10+, Chrome / Chromium / Edge або Brave.

  chmod +x tools/build_mac.sh запуск.command
  ./tools/build_mac.sh

Результат: dist/WoTB Stats by Clark.app
Бери саме .app. Поруч може лежати тека з тим самим ім'ям — це проміжний one-dir.

Підпису Apple немає. Gatekeeper напише, що розробника не перевірено.
Це нормально для своєї збірки.

Перший запуск .app
------------------
1. Скопіюй .app із dist у домашню теку або в Програми. Не запускай із архіву.
2. ПКМ по .app → Відкрити → Відкрити.
   Або: Системні параметри → Конфіденційність і безпека → Open Anyway
   (українською: «Все одно відкрити»).
3. Якщо macOS каже «пошкоджено» після завантаження з інтернету:

   xattr -dr com.apple.quarantine "/шлях/WoTB Stats by Clark.app"

Вікно статистики відкриває Chrome (або Chromium, Edge, Brave).
Іконка в Dock — процес сервера. Хрестик на вікні Chrome зупиняє і сервер.

Реплеї WoT Blitz (офіційно, WG, стаття 22278)
----------------------------------------------
Steam-клієнт:
  ~/Library/Application Support/net.wargaming.wotblitz.macos/DAVAProject/replays

App Store:
  ~/Library/Containers/net.wargaming.wotblitz.macos/Data/Documents/DAVAProject/replays

Тека гри Steam (клієнт і Data, не реплеї):
  ~/Library/Application Support/Steam/steamapps/common/World of Tanks Blitz/

Якщо реплеї не видно: ⚙ Шляхи в застосунку, або Full Disk Access для .app
(Контейнер App Store macOS часто закритий для чужих програм).

data/
-----
Не копіюй data/ з Linux чи Windows.
Каталог створюється при старті. У зібраному .app записи йдуть поруч із .app
або в ~/Library/Application Support/WoTB Stats by Clark/.
Тримай .app у домашній теці або в Програмах і не запускай його прямо з архіву.

Запасний запуск без .app: подвійний клік запуск.command
(теж може спитати Gatekeeper — ПКМ → Відкрити).
Потрібен Python 3.10+ і: python3 -m pip install -r requirements.txt
Поруч вище має лежати тека cl_platform/.
