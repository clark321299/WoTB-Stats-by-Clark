WoTB Stats by Clark — Windows 10 / 11
======================================

Другу віддавай зібрану теку з exe. Python і setup_windows.bat йому не потрібні.
setup_windows.bat — лише тобі, один раз, щоб зібрати цю теку.

Зібрана програма (рекомендовано)
--------------------------------
На Windows один раз:

  tools\build_win.bat

Потрібен Python 3.10+ у PATH (або venv після ..\setup_windows.bat).
Скрипт ставить залежності й збирає one-dir без консолі.

Результат:

  dist\WoTB Stats by Clark\WoTB Stats by Clark.exe

Копіюй і запускай усю теку «WoTB Stats by Clark», не один exe.
Поруч мають лишатися web\, tools\ і бібліотеки збірки.
data\ і chrome_profile\ програма створить сама біля exe
(профілі, база, вікно Chrome/Edge). Їх не замінюй при оновленні exe.

Перший запуск
-------------
1. Встанови Chrome або Edge, якщо їх ще немає.
2. Запусти «WoTB Stats by Clark.exe».
3. Має відкритися вікно додатка на http://127.0.0.1:17321
4. У тій самій теці з'являться data\ і chrome_profile\.
5. Реплеї підхоплюються самі (шляхи нижче). Інший каталог —
   Settings у застосунку або змінна CL_STATS_GAME_DATA_DIR.

Не копіюй data\ з Linux (там шляхи Proton).
Windows 7 / 8 не підтримуються.

SmartScreen («Підпис невідомий»)
--------------------------------
Збірка без кодового підпису. Windows може показати
«Windows захистив ваш ПК» / видавець невідомий.

  Додаткові відомості → Усе одно запустити

Якщо антивірус видаляє або ховає exe — це типовий хибний
спрацьовувач PyInstaller, не вірус. Додай усю теку збірки
у винятки Microsoft Defender і не качай exe з невідомого місця
замість власної збірки.

Запасний запуск із джерел (без exe)
-----------------------------------
  1) Один раз:  ..\setup_windows.bat
  2) Вікно:     запуск.bat
     Лише сервер: запуск-фон.bat

Реплеї, які код шукає на Windows
--------------------------------
  %LOCALAPPDATA%\wotblitz\DAVAProject\replays
  %LOCALAPPDATA%\wotblitz\replays
  D:\Steam\steamapps\common\World of Tanks Blitz\replays

Дані гри (танки) додатково: теки Steam із libraryfolders.vdf
і %ProgramFiles(x86)%\Steam\...\World of Tanks Blitz\Data.
Microsoft Store у пошук шляхів не входить — теку реплеїв
можна вказати вручну в Settings.
