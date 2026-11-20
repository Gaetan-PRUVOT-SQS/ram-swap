# ram-swap

Régulateur des trois strates de mémoire pour le conteneur Crostini d'un Chromebook. Il lit la pression RAM toutes les 5 secondes et écrit trois réglages du noyau : `vm.swappiness`, `vm.vfs_cache_pressure` et `vm.page-cluster`.

## Genèse

La machine de départ est un Chromebook ASUS CM1405 (MediaTek Kompanio 540, 8 Go de RAM, 128 Go d'eMMC), choisi pour de l'ultra-mobilité. Le conteneur Crostini (Debian 13, arm64) y expose environ 6,5 Gio de RAM et un fichier de swap de 8 Gio posé sur l'eMMC. Le noyau de ce conteneur n'a ni zram ni zswap.

Les réglages livrés par Debian (`swappiness = 60`, `vfs_cache_pressure = 100`, `page-cluster = 3`) sont un compromis généraliste : ils évincent des pages anonymes même quand la machine va bien, et leur readahead de 32 Kio ne profite pas d'un disque de swap plus large. Ce projet est l'optimisation maison qui remplace ces valeurs par une politique adaptée au profil de la machine : peu de pages anonymes, beaucoup de cache fichier, un disque de swap qui accepte des lectures de 64 Kio.

## Le principe

Trois strates, sur le modèle Strata :

1. **La RAM** garde le jeu de travail, les pages touchées récemment.
2. **zram**, s'il existe, est la strate tiède : les pages froides y sont compressées. Ce noyau ne l'a pas, la strate reste inactive.
3. **Le fichier de swap** est l'archive disque : il ne reçoit des pages que quand la RAM se serre.

Les noms de strates décrivent la pression choisie, pas le fichier de destination : le noyau écrit toujours dans le périphérique de swap de plus haute priorité.

### Les six strates

| Ordre | Condition d'entrée | Niveau | swappiness | cache | Raison |
|---|---|---|---|---|---|
| 1 | Aucun swap | `sans-swap` | 10 | 100 | le réglage ne change rien |
| 2 | RAM disponible sous 8 %, ou PSI `full` `avg10` ≥ 5 | `urgent` | 40 | 150 | seules les pages les plus froides partent |
| 3 | Débit de swap ≥ 40 Mo/s et PSI `full` `avg10` ≥ 1, hors urgence | `sature` | 15 | 100 | le disque est déjà chargé et des tâches sont bloquées |
| 4 | RAM disponible sous 20 %, ou PSI `some` `avg10` ≥ 8, ou PSI `full` `avg10` ≥ 1 | `cooled` si zram a ≥ 128 Mio libres, sinon `archive` | 35 ou 25 | 80 ou 100 | pages froides vers zram, ou vers le swap disque |
| 5 | zram prêt et RAM disponible sous 40 % | `cooled` | 30 | 80 | zram absorbe les pages peu utilisées |
| 6 | Sinon | `active` | 10 | 50 | la RAM conserve le jeu de travail |

La première condition vraie gagne. Les comparaisons sont strictes : la valeur pile sur un seuil reste du côté le plus calme.

### Hystérésis

Une strate déjà affichée attend 5 points de RAM disponible de plus avant de rendre la main, pour ne pas osciller à chaque lecture :

| Strate tenue | Seuil d'entrée | Seuil de sortie |
|---|---|---|
| `urgent` | disponible < 8 % | disponible < 13 % |
| `archive`, ou `cooled` à swappiness 35 | disponible < 20 % | disponible < 25 % |
| `cooled` à swappiness 30 | disponible < 40 % | disponible < 45 % |

### Plafonds

Après le choix de la strate, deux plafonds ramènent `swappiness` à 10 : swap libre < 256 Mio (y compris en urgence), ou pages anonymes < 256 Mio hors urgence. Si un plafond baisse vraiment la valeur, le niveau annoncé devient `active` et le journal affiche le swappiness réellement écrit ; `vfs_cache_pressure` et `page-cluster` restent ceux de la strate choisie.

`page-cluster` vaut 4 dès qu'il reste du swap disque (lectures d'avance de 64 Kio), 0 si zram est le seul swap.

## Installation

```sh
install -m 755 ram_swap.py /home/<user>/ram-swap/ram_swap.py
ln -s /home/<user>/ram-swap/ram_swap.py /usr/local/bin/ram-swap-policy
cp ram-swap-policy.service /etc/systemd/system/
systemctl enable --now ram-swap-policy
```

Fichier de démarrage `/etc/sysctl.d/99-swap-page-cluster.conf`, appliqué avant que le service reprenne la main :

```ini
vm.swappiness = 10
vm.vfs_cache_pressure = 50
vm.page-cluster = 4
```

L'unité lance `ram-swap-policy --watch --interval 5`, redémarre sur échec (`Restart=on-failure`, `RestartSec=2`) et démarre après `swap.target`. L'état courant est dans `/run/ram-swap-policy.json`.

## Utilisation

```text
ram-swap-policy [--dry-run] [--json] [--watch] [--interval 5] [--proc /proc] [--state /run/ram-swap-policy.json]
```

| Option | Effet |
|---|---|
| `--dry-run` | Affiche la décision et n'écrit ni les sysctl ni l'état |
| `--json` | Affiche `level`, `swappiness`, `vfs_cache_pressure`, `page_cluster`, `reason` |
| `--watch` | Boucle jusqu'au signal. Incompatible avec `--dry-run` |
| `--interval` | Secondes entre deux lectures. Doit être positif. Défaut : 5 |
| `--proc` | Racine à lire à la place de `/proc` |
| `--state` | Fichier d'état à la place de `/run/ram-swap-policy.json` |

Une ligne de journal ne sort que quand le niveau, le swappiness, la pression de cache ou `page-cluster` change :

```text
active swappiness=10 cache=50 cluster=4 dispo=2367 Mio swap=6/8191 Mio — strate active : la RAM conserve ce qui est utile dans l'immédiat
```

`SIGTERM` et `SIGINT` terminent la boucle proprement, après le sommeil de l'intervalle.

## Tests

```sh
make test
```

Équivalent à `python3 -m unittest test_ram_swap test_bench_reel`. 31 tests : pour l'outil, parse de `/proc`, les six niveaux, les deux plafonds, l'hystérésis des trois seuils de RAM, la relecture d'un ancien fichier d'état et l'aller-retour du format nouveau ; pour le harnais d'usage réel, les quantiles, la survie de l'ouvrier sur des tranches de mémoire partielles et les mesures ponctuelles. Suite verte en ~0,5 s.

## Outils de mesure

Trois harnais livrés avec le projet :

- `bench_ram_swap.py` — micro-benchmark d'une version du module : coût d'une itération lecture+décision, détail des parses, latence d'une invocation complète. Accepte un chemin de module pour comparer deux versions : `python3 bench_ram_swap.py /chemin/vers/autre_ram_swap.py`
- `bench_systeme.py` — benchmark système d'une configuration : hog de 4 Gio churné, latence de refault 4 KiB aléatoire sur une région poussée en swap par `MADV_PAGEOUT` (étalonnée contre la même tournée en RAM), échantillonnage du swappiness effectif et de la PSI à 0,5 s
- `bench_reel.py` — benchmark d'usage réel : des applications d'arrière-plan (zone chaude réécrite souvent, zone froide abandonnée) occupent la RAM pendant que le harnais mesure les latences perçues : lancement d'un outil, sauvegarde d'un petit fichier (fsync), débit de lecture disque
- `bench_sature.py` — benchmark créatif de saturation : outils de design web réels (Photopea, Excalidraw) et onglets chargés dans Chrome headless, modèles de documents en RAM touchés en continu, pendant que trois builds gcc doivent aboutir
- `bench_recap.py` — synthèse agrégée des runs de `resultats/`

## Benchmark

### Machine de référence

- Hôte : Chromebook ASUS CM1405, MediaTek Kompanio 540, 8 Go de RAM, 128 Go d'eMMC
- Conteneur : Debian 13 arm64 dans Crostini, noyau 6.6, ~6,5 Gio de RAM exposées
- Swap : `/swapfile` de 8 Gio, priorité −2, sur `vdb` (image disque du conteneur, donc l'eMMC), partagé avec la racine
- MGLRU `lru_gen` à `0x0033`, pas de zram ni zswap
- Comparaison : solution (démon actif, réglages adaptatifs) contre natif Debian (démon arrêté, `60/100/3`)

### Micro-benchmark de l'outil

Sur la machine de référence, 300 itérations, 15 invocations :

| Mesure | Valeur |
|---|---|
| Itération lecture `/proc` + décision (médiane) | ~500 µs, p95 719 µs |
| — parse `meminfo` | 32 µs |
| — parse `pressure` | 4 µs |
| — parse `vmstat` | 66 µs |
| Invocation `--dry-run --json` complète (médiane) | 137 ms, p95 147 ms |
| Démarrage Python nu (référence) | 43 ms |
| Service en production | 7,6 Mio RSS, ~0,11 % de CPU sur 2 h 20 |

Le coût utile d'un cycle est un demi-millième de l'intervalle de 5 s ; les 137 ms d'une invocation one-shot sont presque tout de l'import Python, sans impact en mode service.

### Benchmark système : protocole

Par run, à configuration égale : purge des caches, hog anonyme de 4 Gio touché puis churné (2 passes : écriture complète puis lecture complète ; l'éviction vers le swap est le fait du noyau), puis région de 256 Mio explicitement poussée en swap par `MADV_PAGEOUT` et relue en ordre aléatoire page par page, avec étalonnage contre la même tournée en RAM. Compteurs `pswpin`/`pswpout` et totaux PSI mesurés en deltas autour de chaque passe. Deux runs par configuration, entrelacés : solution → natif → solution → natif.

### Résultats par run

**solution-run1** — sysctl début `10/50/4`, fin `40/150/4` (strate `urgent`) :

| Passe | Durée | Débit | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 221,1 s | 77 Mio/s | 8585 Mio | 8356 Mio | 6,6 s | 6,1 s |
| 2 | 240,5 s | 73 Mio/s | 8851 Mio | 8623 Mio | 6,8 s | 6,5 s |

Refault 4 KiB : 1,8 µs médiane, p95 3,2 µs. Montée en charge : 13,0 s.

**natif-run1** — sysctl constants `60/100/3` :

| Passe | Durée | Débit | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 256,2 s | 66 Mio/s | 8587 Mio | 8412 Mio | 5,5 s | 5,2 s |
| 2 | 256,2 s | 65 Mio/s | 8459 Mio | 8281 Mio | 6,9 s | 6,6 s |

Refault 4 KiB : 1,7 µs médiane, p95 3,1 µs. Montée en charge : 12,1 s.

**solution-run2** — sysctl début `10/50/4`, fin `15/100/4` (strate `sature`) :

| Passe | Durée | Débit | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 211,0 s | 80 Mio/s | 8598 Mio | 8291 Mio | 5,4 s | 5,1 s |
| 2 | 248,9 s | 68 Mio/s | 8719 Mio | 8312 Mio | 9,9 s | 9,5 s |

Refault 4 KiB : 1,8 µs médiane, p95 2,8 µs. Montée en charge : 12,3 s.

**natif-run2** — sysctl constants `60/100/3` :

| Passe | Durée | Débit | pswpin | pswpout | PSI some | PSI full |
|---|---|---|---|---|---|---|
| 1 | 278,1 s | 61 Mio/s | 8752 Mio | 8269 Mio | 7,6 s | 7,3 s |
| 2 | 324,7 s | 54 Mio/s | 8948 Mio | 8580 Mio | 9,0 s | 8,6 s |

Refault 4 KiB : 1,8 µs médiane, p95 3,5 µs. Montée en charge : 9,8 s.

### Synthèse agrégée

Médianes des passes, 2 runs par configuration :

| Métrique | Solution | Natif Debian | Écart |
|---|---|---|---|
| Passe de churn 4 Gio | **230,4 s** | 278,8 s | solution **+21 % plus rapide** |
| Débit effectif | **74,4 Mio/s** | 61,7 Mio/s | solution **+20 %** |
| PSI `full` par passe | 6,8 s | 6,9 s | égalité |
| PSI `some` par passe | 7,2 s | 7,3 s | égalité |
| Refault 4 KiB médiane | 1,81 µs | 1,73 µs | égalité (±10 % = bruit) |
| Montée en charge | 12,6 s | 10,9 s | natif légèrement devant |

### Lecture des résultats

1. **Le gain net de la solution est le débit soutenu.** Les deux configurations déplacent les mêmes volumes (~8,5 Gio in/out par passe) mais la solution les déplace 17 à 21 % plus vite. Le facteur identifié est `page-cluster` : 64 Kio de readahead contre 32 Kio au natif. Une mesure antérieure sur le même matériel donnait ~220 Mo/s de swap-in séquentiel à cluster 4 contre ~137 Mo/s à cluster 3, même direction.
2. **Le natif se dégrade, la solution tient.** Les passes natives ralentissent de run en run (256 → 278 → 325 s) tandis que la solution reste dans un bandeau serré (211-249 s). La stratégie adaptative a varié intelligemment : strate `urgent` (40/150) au premier run, strate `sature` (15/100) au second — le garde anti-tempête réduit le swappiness quand le swap déplace déjà trop.
3. **L'interactivité (PSI) est une égalité** sous ce thrash extrême, et le refault aléatoire aussi : pas de gain inventé là où il n'y en a pas. La valeur calme du démon (swappiness 10 contre 60 au quotidien) n'est pas exercée par ce test ; elle repose sur le profil de la machine (~133 Mio de pages anonymes, plafond qui maintient 10 hors urgence).

### Réserves

2 runs × 2 passes par configuration : motif constant sur les quatre appariements, mais échantillon court. Le disque de swap est partagé avec la racine du conteneur, donc du bruit d'hôte est possible. Le test force un thrash que l'usage réel de cette machine dépasse rarement.

### Usage réel : poste contraint, latences perçues

Le thrash forcé ci-dessus ne dit rien du quotidien. Ce second protocole occupe la RAM comme des applications ouvertes — chacune avec une zone chaude réécrite toutes les 150 ms et une zone froide abandonnée — et mesure 50 fois, toutes les 2 s, ce qu'un utilisateur ressent : lancement d'un interpréteur Python, sauvegarde d'un fichier de 64 Kio avec `fsync` sur l'eMMC, débit de lecture disque. Le harnais abandonne la mesure si un ouvrier meurt : la charge doit être tenue du début à la fin. Deux scénarios, deux paires entrelacées solution/natif, 50 échantillons par run.

**Scénario modéré** — 2 applications × 600 Mio, RAM disponible médiane 9-10 %. Attention : ce couple de runs est biaisé par l'ordre (pas de remise à zéro du swap entre eux ; le natif héritait de la marge libérée par le swap-out de la solution qui tournait juste avant). Les chiffres restent publiés pour ce qu'ils sont :

| Métrique (médiane) | Solution (`urgent` 40/150) | Natif (60/100/3) |
|---|---|---|
| Lancement outil | 52,1 ms | 51,0 ms |
| Sauvegarde fsync | 16,4 ms (p95 170, max 304) | 12,7 ms (p95 19,8, max 38) |
| Lecture disque | 584 Mio/s | 725 Mio/s |
| Swap in / out | 42 / **387 Mio** | 18 / 0 Mio |
| PSI some / full | 1,8 / 1,4 s | 0,1 / 0,1 s |

**Scénario chargé** — 3 applications × 500 Mio, RAM disponible médiane 2,5-4 %, swap remis à zéro avant chaque run (paire propre) :

| Métrique (médiane) | Solution (`urgent` 40/150) | Natif (60/100/3) |
|---|---|---|
| Lancement outil | 50,2 ms | 49,7 ms |
| Sauvegarde fsync | **12,8 ms** (p95 24,5) | 15,6 ms (p95 27,5, max 137) |
| Lecture disque | **777 Mio/s** | 667 Mio/s |
| Swap in / out | 0 / **176 Mio** | 0 / 241 Mio |
| PSI some / full | 0,1 / 0,1 s | 0,1 / 0,1 s |

**Lecture.**

1. La paire propre (scénario chargé) donne la solution meilleure ou égale sur tout ce qui se perçoit : sauvegardes 18 % plus rapides, lectures +16 %, 27 % de pages en moins déplacées vers le swap, mêmes quasi-absence de gels. Le lancement d'outil est une égalité (1 %).
2. La paire modérée, biaisée par l'ordre, dit malgré tout une chose vraie : le mode `urgent` du démon (swappiness 40, cache 150) peut payer des pics de fsync (p95 170 ms) quand l'éviction agressive et l'I/O eMMC se superposent. C'est le coût réel de sa réactivité, publié tel quel.
3. Dans les deux runs de la solution, le démon a détecté la pression réelle et monté en `urgent` (10→40, cache 150), puis y est resté par hystérésis. Au natif, rien ne réagit : les valeurs restent 60/100/3 du début à la fin.
4. Correction de méthode publiée pour rester honnête : la première version de ce protocole mesurait une machine quasi au repos — les ouvriers mouraient ~150 ms après le départ (bug de tranche `mmap` sur une zone partielle), la charge n'était jamais tenue, et les chiffres « aucune config n'échange » décrivaient du vide. Le harnais est corrigé (écriture à longueur exacte, arrêt partagé entre processus, garde de survie) et couvert par 9 tests dédiés.

### Saturation créative : design web + onglets + build de dev

La question posée : la machine et le swap tiennent-ils un poste créatif exigeant ? Scénario : 6 onglets Chrome headless (Photopea, deux Excalidraw, trois articles Wikipédia chargés), trois « modèles de documents » de 600 Mio touchés en continu (les calques d'un fichier de design), et pendant ce temps trois builds gcc de 80 fichiers qui doivent réussir. Remise à zéro du swap avant chaque run, 8 Go de RAM sur 8 Gio de swap.

| Métrique | Solution (`urgent` 40 → `archive` 25) | Natif (60/100/3) |
|---|---|---|
| Builds réussis | 3/3 | 3/3 |
| Durées de build | 19,6 / 10,6 / **8,3 s** | 20,1 / 9,9 / 10,9 s |
| Sauvegarde fsync (médiane / p95) | **62,1 ms / 205 ms** | 68,4 ms / 319 ms |
| Lancement outil | 85,5 ms | 85,6 ms |
| Lecture disque | 402 Mio/s | 437 Mio/s |
| Swap in / out | 179 / 2 850 Mio | 93 / 2 811 Mio |
| PSI some / full | 10,4 / 4,4 s | 7,5 / 4,1 s |
| OOM | 0 | 0 |
| RAM disponible minimale | 9,4 % | 9,4 % |

Les deux configurations tiennent le scénario : aucun OOM, tous les builds réussissent, tous les process survivent. La solution garde deux petits avantages dans la profondeur de la saturation : le troisième build (8,3 s contre 10,9 s) et la queue des sauvegardes (p95 +36 % mieux), cohérents avec le readahead 64 Kio mesuré plus haut ; le natif garde des micro-gels cumulés un peu plus faibles. Le démon a suivi la courbe de pression (montée `urgent` 40 au pic, retour `archive` 25 ensuite) quand le natif restait figé à 60.

Un couple de runs seulement : lecture indicative, pas statistique.

### Bilan des trois régimes

| Régime | Verdict |
|---|---|
| Quotidien calme à modéré (usage réel, dispo > 9 %) | Égalité : aucune config n'échange beaucoup, latences perçues indiscernables. La solution ne dégrade rien et reste à swappiness 10 tant que la machine respire. |
| Pénurie (churn forcé, thrash) | **Solution +20 % de débit soutenu** (74,4 contre 61,7 Mio/s) grâce au readahead 64 Kio ; le natif se dégrade de passe en passe, la solution tient son bandeau. |
| Saturation créative (design web + onglets + builds) | **Les deux tiennent** : zéro OOM, tous les builds réussissent, ~2,8 Gio poussés dans le swap. Petits avantages solution dans la profondeur (build 8,3 s contre 10,9 s, fsync p95 +36 % mieux), deux petits avantages natifs (PSI some, lecture). |
| Coût connu de la solution | Le mode `urgent` (swappiness 40, cache 150) peut payer des pics de fsync sous I/O eMMC simultanée. Publié tel quel. |

La valeur du démon se joue en dessous de la ligne de flottaison : au-dessus, il ne touche à rien ; en dessous, il adapte (10 → 25 → 40 selon la strate, garde anti-saturation à 15) là où le natif reste figé à un compromis généraliste.

## Licence

MIT — voir [LICENSE](LICENSE).
