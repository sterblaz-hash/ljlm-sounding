# LJLM Sounding – Health v2: samodejna objava

To je **naslednji korak po Health v2**. ZIP vsebuje:

- `build_system_health.py`: dosedanji pregledovalnik z majhnim popravkom, ki **zgodovinske arhivske vrzeli** loči od **trenutnega** zdravja sistema;
- `test_system_health.py`: dosedanjih 24 testov in tri nove regresijske teste;
- `.github/workflows/update-system-health.yml`: nov samostojen workflow GitHub Actions.

## Varnost in obnašanje

- `workflow_dispatch` za ročni preizkus; `schedule` vsakih 3 ure ob :17 UTC; samodejni urnik teče na privzeti veji (`main`).
- Pred objavo izvede **27 offline testov**.
- Za pripravo JSON uporablja `python build_system_health.py --github-actions --days 7`.
- Prebere štiri GitHub Actions workflowe prek GitHub API s priloženim `GITHUB_TOKEN` in pravico `actions: read`. Če poizvedba spodleti, poročilo označi dostop kot `unavailable` – ne sklepa o vzroku.
- Objavi **samo** `data/system_health.json`, ničesar ne spreminja v opazovanjih, modelskih produktih, klimatologiji ali arhivih IGRA.
- Pred vsako objavo obnovi sveže podatke iz oddaljene veje in ob sočasni posodobitvi ponovi izračun; brez prisilnega `push`.
- `git reset --hard` je **samo znotraj začasnega GitHub Actions delovnega imenika**; nikoli ga ne izvajaj ročno na svojem računalniku.
- JSON vsebuje `checked_at`, zato se ob rednih zagonih praviloma ustvari nov commit tudi, kadar je stanje nespremenjeno. To je namerno zaradi časa zadnjega preverjanja (približno 8 commitov/dan).
- GitHubov urnik ni zagotovljen do minute; zamude sprožilcev so mogoče.
- Ne zaganja modelov in ne izvaja sanacij ali ponovnih ekstrakcij.

## Lokalna namestitev

```bash
cd ~/ljlm-sounding-ops-fix
cp build_system_health.py /tmp/build_system_health_v2.py
cp test_system_health.py /tmp/test_system_health_v2.py
unzip -o "$(xdg-user-dir DOWNLOAD)/ljlm_health_publisher.zip" -d .
python -m unittest test_system_health -q
python build_system_health.py --days 7
python -m json.tool data/system_health.json >/dev/null
git diff --check
git status --short
```

Pričakovano: **27 testov, OK**. Po uspešni lokalni preverbi objavi nove datoteke prek ločene razvojne veje in Pull Requesta. Datoteke `data/system_health.json` **še ne dodajaj v commit ročno**: to naredi nov workflow po združitvi v `main` in prvem sproženem zagonu.

## Po združitvi

GitHub → Actions → **Publish LJLM system health** → **Run workflow**, veja `main`.

Uspešen zagon preveri v korakih `Run offline health tests` in `Refresh health report and publish only its JSON`. Nato `data/system_health.json` postane dostopen na `main` in ga lahko vključiš v spletni dashboard.

### Posebej pomembno za prikaz

- `status`: **trenutno** stanje.
- `checks`: sedem trenutnih virov (OBS, ICON-D2, C-LAEF, C-LAEF point).
- `archive.status`, `archive.gaps`: **zgodovina** vrzeli v zadnjih sedmih dneh.
- `github_actions`: nedavne izvedbe in opozorila; brez ugibanja o vzroku napak.
- `checked_at`: čas zadnje osvežitve.

Roki objave so še vedno **okvirni** (OBS/ICON-D2 4 ure, C-LAEF 7 ur po veljavnem terminu) in jih bo treba pozneje prilagoditi operativnim urnikom.
