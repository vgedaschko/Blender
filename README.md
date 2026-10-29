# Leopard 2A8 – Blender-Modell für Unreal Engine 5

Ein prozedural erzeugtes, geriggtes Echtzeit-Modell eines Leopard 2A8 als Blender-Python-Skript,
mit fertigen Exporten für UE5 (Skeletal Mesh, Nanite-taugliches Static Mesh, Kettenglied, Kettenpfad).

![Front](renders/leopard2a8_front34.jpg)
![Heck, Turm geschwenkt](renders/leopard2a8_rear34.jpg)
![Seitenansicht orthografisch](renders/leopard2a8_side_ortho.jpg)

## Genauigkeit: was das Modell ist und was nicht

**Es ist kein CAD-genaues Modell.** Die Konstruktionsdaten des Leopard 2A8 gehören KNDS Deutschland und sind
zum Teil eingestuft. Aus öffentlichen Quellen lässt sich keine CAD-genaue Geometrie ableiten.

Das Modell hält sich an öffentlich bekannte Eckdaten und ist dazwischen nach Erscheinungsbild angenähert:

| Merkmal | Modell | öffentliche Angabe (ca.) |
|---|---|---|
| Länge, Rohr nach vorn | 11,02 m | ~10,97 m |
| Breite inkl. Schürzen/Trophy | 3,82 m | ~3,77–3,83 m |
| Höhe (ohne Antennen) | 3,05 m | ~3,0 m |
| Bodenfreiheit | 0,48 m | ~0,49–0,54 m |
| Laufwerk | 7 Laufrollen Ø 700 mm, 4 Stützrollen, Triebrad hinten | wie Original |
| Kette | 635 mm breit, 85 Glieder je Seite | 635 mm |
| Kanone | Rh-120 L/55A1 (6,6 m) mit Rauchabsauger und Mündungsreferenzsystem | wie Original |

Alle Werte stehen am Anfang von `leopard2a8/build_leopard2a8.py` als Konstanten und lassen sich anpassen.
Die gemessenen Werte jedes Builds landen in `export/model_info.json`.

Angenäherte bzw. vereinfachte Bereiche: Turm- und Keilpanzerungsform, Sichtgeräte (EMES 15, PERI R17A3),
Waffenstation FLW 200, Trophy-Werfer und Radarpaneele, Motordeck, Heck, Schwingarme.

## Inhalt

```
leopard2a8/build_leopard2a8.py   Generator (Geometrie, UVs, Materialien, Rig, Export, Vorschau)
export/Leopard2A8.blend          Blender-Szene (Collections je Baugruppe, Rig, Kamera/Licht)
export/SK_Leopard2A8.fbx         Skeletal Mesh für UE5 (41 Bones, Kettenband für UV-Panner)
export/SM_Leopard2A8.fbx         Static Mesh (Einzelkettenglieder) + UCX-Kollision, z. B. für Nanite
export/SM_Leopard2A8_TrackLink.fbx  einzelnes Kettenglied für Spline-/Instanz-Ketten
export/track_path_ue.json        Kettenmittellinie je Seite in UE-Koordinaten (cm) für Splines
export/model_info.json           gemessene Maße, Dreieckszahlen, Bone-Liste
renders/                         Vorschaubilder (Cycles)
```

Dreiecke: Wanne ~26k, Turm+Kanone ~24k, Laufwerk ~43k, Kettenband 4k (UE-Skeletal ≈ 98k);
Static Mesh mit Einzelgliedern ≈ 119k.

## Neu erzeugen

Voraussetzung: Blender ≥ 4.2 (getestet mit 5.2 LTS) oder das pip-Modul `bpy` (Python 3.13).

```bash
blender -b -P leopard2a8/build_leopard2a8.py -- --out export --renders renders
# oder
pip install bpy && python leopard2a8/build_leopard2a8.py --out export --renders renders
```

Optionen: `--no-export`, `--no-render`, `--samples N`. Ein Durchlauf ohne Rendern dauert etwa 5 Sekunden.
Das Skript lässt sich auch im Blender-Texteditor mit „Run Script“ ausführen.

## Aufbau

**Koordinaten:** Blender +X = Front, +Y = links, +Z = oben, 1 Einheit = 1 m, Nullpunkt am Boden unter der
Wannenmitte. Der Export nutzt `FBX_SCALE_ALL`, damit das Fahrzeug in UE5 in cm ankommt, nach +X zeigt und
Skalierung 1,0 hat. Das Armature-Objekt heißt `Armature`, damit UE5 keinen zusätzlichen Root-Bone anlegt.

**Materialien = Texture-Sets** (für Substance Painter / UE5):

| Material | Inhalt |
|---|---|
| `MI_L2A8_Hull` | Wanne, Schürzen, Motordeck, Heck |
| `MI_L2A8_Turret` | Turm, Optiken, Luken, FLW 200, Trophy |
| `MI_L2A8_Gun` | Blende und Rohr |
| `MI_L2A8_RunningGear` | Lauf-, Stütz-, Leit- und Triebräder, Schwingarme |
| `MI_L2A8_Track` | Kette (Band bzw. Glieder) |
| `MI_L2A8_Optics` | Glasflächen |

Jede Gruppe hat ihren eigenen, überlappungsfreien UV-Atlas. Die Oberflächenart (Lack, Stahl, Gummi, dunkel,
Glas) steckt als Vertex-Farbe `ColorID` im Mesh. In Substance Painter dient sie als ID-Maske, in UE5 kann sie
direkt im Material genutzt werden. Die Tarnfarbe in den Blender-Renderings ist ein prozeduraler Shader und
wird nicht exportiert. In UE5 kommt nur die Grundfarbe an.

**Bones** (alle unter `root`):

| Bone | Zweck |
|---|---|
| `turret` | Turm-Seitenrichtung (Drehung um Z) |
| `gun` → `barrel` → `muzzle` | Höhenrichtung am Schildzapfen; `barrel` für den Rohrrücklauf; `muzzle` als Socket für Mündungsfeuer/Projektil |
| `coax` | Socket Koax-MG |
| `rws_yaw` → `rws_pitch` → `rws_muzzle` | Waffenstation FLW 200 |
| `hatch_commander`, `hatch_loader` | Turmluken (Gelenk an der hinteren Kante) |
| `trophy_l`, `trophy_r` | drehbare Trophy-Werfer |
| `wheel_[l/r]_01..07`, `idler_*`, `sprocket_*`, `roller_[l/r]_01..04` | Räder (Drehung um Y) |
| `exhaust_l`, `exhaust_r` | Sockets für Abgas-Partikel |

Alle Teile sind starr mit Gewicht 1,0 an genau einen Bone gebunden.

## Import in UE5

1. **Skeletal Mesh:** `SK_Leopard2A8.fbx` importieren mit *Skeletal Mesh* an, *Import Mesh* an,
   *Normal Import Method: Import Normals*, *Import Animations* aus, *Create Physics Asset* an,
   *Material Import Method: Create New Materials*, *Vertex Color Import Option: Replace*.
2. **Physics Asset:** Die automatisch erzeugten Körper auf eine Box für `root` (Wanne), eine für `turret`
   und eine Kapsel für `barrel` reduzieren. Für Räder Kugeln bzw. die Fahrzeugkomponente verwenden.
3. **Fahrzeug:** Eine Chaos-Vehicle-Komponente oder ein eigener Kettenantrieb kann `wheel_*`, `sprocket_*`,
   `idler_*` und `roller_*` direkt drehen. Turm und Kanone im Animation Blueprint über *Transform (Modify) Bone*
   steuern (`turret` Yaw, `gun` Pitch).
4. **Kette, Variante A (einfach):** Das Kettenband nutzt pro UV-Kachel in V genau ein Kettenglied. Ein
   Panner auf der V-Koordinate, gekoppelt an die Fahrgeschwindigkeit
   (`V-Offset = gefahrene Strecke / 18,43 cm`), lässt die Kette laufen. Als Textur eignet sich ein
   Normal/Height-Bake von `SM_Leopard2A8_TrackLink`.
5. **Kette, Variante B (detailliert):** `SM_Leopard2A8_TrackLink.fbx` importieren und mit den Punkten aus
   `track_path_ue.json` je Seite einen geschlossenen Spline aufbauen. Die Glieder per Instanced Static Mesh
   gleichmäßig verteilen (85 Glieder je Seite, Teilung 18,43 cm) und entlang des Splines verschieben.
   Die Punkte liegen bereits in UE-Koordinaten (cm, Y gespiegelt).
6. **Static Mesh / Nanite:** `SM_Leopard2A8.fbx` mit *Combine Meshes* importieren. Die `UCX_`-Hüllen werden
   automatisch zur Kollision. Nanite kann aktiviert werden (z. B. für Wracks oder Set-Dressing).

## Bekannte Grenzen

- Schwingarme und Kettenband hängen am `root`-Bone. Bei echter Federung bewegen sich Räder, Arme und Kette
  nicht gemeinsam. Abhilfe: zusätzliche Federungs-Bones und Gewichte für das Kettenband.
- Die obere Kettenlinie liegt gerade auf den Stützrollen auf, es gibt keinen Durchhang.
- Es gibt kein Innenleben und keine LODs (UE5 kann LODs automatisch erzeugen; Nanite für das Static Mesh).
- Alle Details sind angenähert (siehe oben). Für Nahaufnahmen empfiehlt sich eine Nachmodellierung nach Fotos.

## Genauer werden

1. **Referenzen:** maßstäbliche Risszeichnungen (Fachliteratur, Modellbau-Pläne) als Hintergrundbilder
   einrichten und die Konstanten im Skript daran ausrichten.
2. **Fotogrammetrie / Laserscan:** an einem zugänglichen Fahrzeug, z. B. bei einem Museum, Tag der offenen
   Tür oder mit Genehmigung der Bundeswehr bzw. eines Nutzerstaats.
3. **Lizenzierte Daten:** Für ein kommerzielles Projekt ist eine Anfrage bei KNDS der einzige Weg zu
   herstellernahen Daten.

## Rechtlicher Hinweis

„Leopard“ und das Fahrzeugdesign können marken- und designrechtlich geschützt sein. Für private und
nicht-kommerzielle Projekte ist das in der Regel unkritisch. Für kommerzielle Spiele oder Produkte vorab
die Rechtslage klären bzw. eine Lizenz beim Hersteller anfragen.
