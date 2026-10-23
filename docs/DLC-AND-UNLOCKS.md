# DLC and unlocks

[← README](../README.md) · [Setup guide](BUILDING.md) · [Game updates](UPDATES.md) · [Multiplayer](MULTIPLAYER.md)

Three separate things are involved:

1. **Telling the game which DLC you own**, with a small list file called `aoc.cfg`.
2. **Building the DLC fighters' code**, which the normal build does by itself.
3. **Unlocking the base-game fighters** on your save, with `launchers\Unlock All Fighters.bat`.

## Contents

- [Which DLC each version has](#which-dlc-each-version-has)
- [Turn on your DLC](#turn-on-your-dlc)
- [Unlock every base-game fighter](#unlock-every-base-game-fighter)
- [Other ways to run the unlock tool](#other-ways-to-run-the-unlock-tool)
- [What the unlock tool changes](#what-the-unlock-tool-changes)

## Which DLC each version has

The game's version decides which DLC fighters it has. For the two tested versions:

| Game version | DLC fighters it can use |
| :--- | :--- |
| **11.0.0** | Piranha Plant, Joker, Hero, Banjo & Kazooie, Terry, Byleth, Min Min, Steve, Sephiroth, Pyra/Mythra. |
| **13.0.5** | All of the above, plus **Kazuya** (added in 12.0.0) and **Sora** (added in 13.0.0). |

No setting or save change can add a fighter that your version's game data doesn't have. To get Kazuya and Sora on an older version, [apply a later update](UPDATES.md) from your own update file.

The 11.0.0 build extracts and compiles the 96 modules in the game archive's `prebuilt:/nro/release/` folder (98 in 13.0.5): fighters, but also shared logic, items and special modes. That doesn't mean every fighter, stage or mode has been tested in a match.

## Turn on your DLC

Without `aoc.cfg`, the game sees no DLC. To make one from your own DLC files:

1. Put your DLC `.nsp` files in a folder, for example `D:\MyDump\dlc`. (An installed-content folder, such as an emulator's `registered` folder, works too.)
2. Open PowerShell in the WindowSmash folder ([how](BUILDING.md#step-6-open-powershell-in-the-windowsmash-folder)) and run:

   ```powershell
   python tools/nca.py aoc-cfg "D:\MyDump\dlc" --keys "D:\MyKeys" --out dump/modules/aoc.cfg
   ```

   It answers `wrote dump/modules/aoc.cfg:` and how many add-ons it found.

3. If you've already built the game, build it again so it matches:

   ```powershell
   python tools/arc.py nro
   python tools/recomp.py all dump/exefs --out build --jobs 8
   ```

   (Use the `--jobs` number from [step 14](BUILDING.md#step-14-build-the-game).)

The next time the game starts, it may announce new fighters, stages, music or costumes. In multiplayer, every player should have the same DLC.

**For a version folder** such as `v13.0.5`, write its own list with `--out v13.0.5/dump/modules/aoc.cfg`, or give `tools/install_version.py` the `--dlc` option, which does it for you ([game updates](UPDATES.md)).

**With a runtime package**, give `launchers\Configure Game Data.bat` an `aoc.cfg` you already have when it asks. Packages never include anyone's DLC list.

## Unlock every base-game fighter

The game starts with only the original eight fighters unlocked. `launchers\Unlock All Fighters.bat` unlocks the rest of the base-game roster on your own save.

1. Start the game once and wait for the main menu, so it creates a save. Then close it.
2. **Make sure every game window is closed.** The game keeps its save in memory and would overwrite the change.
3. Double-click **`launchers\Unlock All Fighters.bat`** in the WindowSmash folder. With [several versions](VERSIONS.md) built, it asks which one.
4. Read what it says. It makes a dated backup of your save in `save_backups\` first: keep it.
5. Start the game again. Unlock notices appear for the new fighters: skip each with the **X** button (<kbd>C</kbd> on the keyboard).

**Mii Fighters** may need to be made first, under **Games & More → Mii Fighters**, before they appear on character select. To give them your own Miis' faces, import your Miis first ([Your own Miis](PLAYING.md#your-own-miis)).

The tool runs with Windows' built-in PowerShell, so it doesn't need Python. It refuses to run while the game is open, works only on 11.0.0 and 13.0.5 saves (it refuses other versions), and replaces the save only after making the backup. Running it again once every fighter is unlocked changes nothing.

**To undo:** close the game, then copy the backup the tool reported back over the `system_data.bin` it names.

## Other ways to run the unlock tool

From PowerShell in the WindowSmash folder. `--dry-run` (`-DryRun`) shows what would change without changing anything:

```powershell
python tools/unlock_all.py --dry-run
python tools/unlock_all.py
python tools/unlock_all.py --tree v13.0.5 --dry-run
python tools/unlock_all.py --save "D:\MySave\system_data.bin" --dry-run
powershell -NoProfile -File tools/Unlock-All-Fighters.ps1 -DryRun
```

- `--tree` (`-Tree` in PowerShell) works on a version folder and its own save. Without it, the tools use the main folder's.
- The tools read which version a folder holds from its program's build ID.
- They know the `system_data.bin` layout of **11.0.0** and **13.0.5**, which share the same offsets, and refuse every other version.
- They don't convert saves between versions and don't install DLC. They never need anyone else's save.

## What the unlock tool changes

Each base-game fighter has a selection and challenger entry, and a separate "joined a battle" milestone. The tool sets the selection flag, clears a pending challenger, gives the fighter an encounter order if it has none, and fills in a missing milestone. Every byte outside those tables stays as it was.

### 13.0.5's save

13.0.5 keeps 11.0.0's layout:

- The save is the same size, with the same format number at offset 4 (`0x1b`). Offset 8 holds the version that last wrote it: `0xb000` for 11.0.0, `0xd005` for 13.0.5. When 13.0.5 loads an 11.0.0 save, it changes that field and a few counters and moves no tables.
- Fighter entries are indexed by `save_no` from `ui/param/database/ui_chara_db.prc`: 0–73 for the base game and 74–83 for the earlier DLC fighters, the same in both versions. 13.0.5 adds 84 (Kazuya) and 85 (Sora), with their milestones after the other DLC fighters'.
- The game sets the DLC fighters' flags itself when their DLC is installed, so the tools leave entries 74 and up alone.

This was checked on copies of saves: on 13.0.5, a partly locked save showed exactly the fighters its flags say, and after the tool ran, character select showed the full roster.
