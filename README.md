# 🎼 AudioScoreTranscriber

> **Transcription audio vers partition directement intégrée dans MuseScore 4.**
> Architecture bicéphale native (Plugin QML MuseScore 4 & Sidecar DSP / Deep Learning Python).

[![MuseScore 4](https://img.shields.io/badge/MuseScore-4.2%2B-blue.svg)](https://musescore.org/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/badge/Status-Alpha%20Active-orange.svg)]()

---

## 🌟 Présentation

**AudioScoreTranscriber** est un plugin de nouvelle génération pour **MuseScore 4**, conçu pour transformer n'importe quel signal acoustique (voix, sifflement, battements de mains, instruments acoustiques, piano polyphonique) en notation musicale gravée directement sur la portée active.

Contrairement aux outils de transcription externes lourds et déconnectés, **AudioScoreTranscriber** s'intègre directement dans le workflow du compositeur grâce à une **liaison bidirectionnelle temps réel** entre l'interface utilisateur de MuseScore et un moteur d'inférence audio Python local.

---

## 🏛️ Architecture Bicéphale (Dual-Engine)

Le projet repose sur une séparation stricte et élégante des responsabilités :

`mermaid
flowchart LR
    subgraph MuseScore [🎼 MuseScore 4 (Frontend UI)]
        Plugin[Plugin QML / JS<br/>(Interface & Score DOM)]
        ScoreDOM[Score Cursor & Elements<br/>(Insertion des Notes)]
        Plugin --> ScoreDOM
    end

    subgraph IPC [⚡ Liaison Asynchrone Localhost]
        WS[WebSocket / IPC Loop<br/>(ws://localhost:8765)]
    end

    subgraph Python [🐍 Sidecar Backend (Inférence & DSP)]
        Capture[Audio Engine<br/>(sounddevice / WASAPI / ASIO)]
        DSP[DSP & Neural Models<br/>(Basic Pitch, pYIN, Onsets)]
        Quantizer[Quantificateur Rythmique & Tonal<br/>(Music21, Partitura)]
        Capture --> DSP --> Quantizer
    end

    Plugin <==> WS
    WS <==> Quantizer
`

1. **Frontend QML (MuseScore 4)** :
   - Panneau de contrôle dockable natif intégré à MuseScore.
   - Sélection du périphérique d'entrée micro, calibration de latence et visualiseur VU-mètre.
   - Déclenchement de l'enregistrement, du métronome et pré-décompte (count-in).
   - Réception des flux de notes quantifiées et insertion chirurgicale via l'API Cursor de MuseScore.

2. **Sidecar Python (Moteur Inférence & DSP)** :
   - Serveur local ultra-réactif communicant par WebSockets.
   - Enregistrement haute fidélité sans latence (WASAPI Exclusive, ASIO, DirectSound).
   - Inférence neuronale et algorithmique spécialisée selon le mode sélectionné.
   - Quantification métrique intelligente (grille harmonique, détection des triolets, gestion du legato/staccato).

---

## 🎯 Les 3 Modes de Transcription

AudioScoreTranscriber propose trois pipelines spécialisés adaptés aux contextes de composition courants :

| Mode | Pipeline Technique | Cible Musicale | Résultat dans MuseScore |
|---|---|---|---|
| **🥁 1. Rythme Seul** | Détection d'attaques par flux spectral (*Spectral Flux & Energy Novelty Curve*) + clustering d'énergie | Beatbox, claquements de mains, percussions corporelles, tapotements de table | Portée rythmique mono-ligne, notation de batterie ou têtes de notes percussives quantifiées. |
| **🎹 2. Piano Polyphonique** | Réseau neuronal convolutif polyphonique (*Spotify Basic Pitch* / architectures hybrides onsets-frames) | Piano acoustique/numérique, guitare acoustique, harpe, ensembles harmoniques | Clé de Sol & Clé de Fa (Grand Staff), détection précise des accords, voix séparées et durées polyphoniques. |
| **🎤 3. Chant Mélodique** | Suivi continu de pitch $ (*pYIN / Crepe*) avec filtre anti-formant et stabilisation de vibrato | Voix chantée, fredonnement, sifflement, flûte, saxophone, violon solo | Ligne mélodique continue, correction tonale intelligente (chromatique ou selon l'armure de la partition), gestion des liaisons. |

---

## 🎧 Overdubbing Synchronisé au Casque

La composition assistée par l'audio exige un timing parfait :
- **Monitoring & Lecture Conjointe** : Le sidecar synchronise le déclenchement de l'enregistrement micro avec la lecture audio de la partition existante (backing track) ou le clic métronome de MuseScore.
- **Compensation Active de Latence** : Calibration automatique du buffer audio aller-retour (round-trip latency) pour éviter tout décalage entre l'écoute au casque et l'interprétation enregistrée.
- **Prises Multiples (Takes)** : Possibilité de réenregistrer une mesure ou un passage précis en boucle et de choisir la meilleure passe de transcription.

---

## 🚀 Installation & Démarrage Rapide

### 1. Prérequis
- **MuseScore 4.2+** installé sur votre machine.
- **Python 3.10+** (recommandé 3.11).
- Un microphone ou une interface audio USB.

### 2. Configuration de la Liaison Live NTFS (Windows)
Pour développer ou utiliser le plugin directement depuis votre dossier de code sans avoir à copier manuellement les fichiers à chaque modification, exécutez le script de jonction NTFS (aucune élévation administrateur requise) :

`cmd
cd C:\Users\Jamet\Documents\code\AudioScoreTranscriber
setup_live_plugin.bat
`

Ce script lie instantanément le répertoire de travail au dossier officiel des plugins de MuseScore 4 :
%USERPROFILE%\Documents\MuseScore4\Plugins\AudioScoreTranscriber $\longleftrightarrow$ C:\Users\Jamet\Documents\code\AudioScoreTranscriber.

### 3. Installation des Dépendances Python

Créez un environnement virtuel dédié et installez les bibliothèques requises :

`powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
`

### 4. Démarrage du Sidecar Backend

Lancez le serveur d'écoute WebSocket :

`powershell
python -m audioscore.server
`

Le serveur écoute par défaut sur ws://localhost:8765.

### 5. Activation dans MuseScore 4

1. Lancez **MuseScore 4**.
2. Allez dans le menu supérieur : **Plugins** $\rightarrow$ **Gérer les plugins...**
3. Cochez **AudioScoreTranscriber**.
4. Ouvrez une partition, puis cliquez sur **Plugins** $\rightarrow$ **AudioScoreTranscriber** pour afficher le dock.

---

## 📂 Structure du Répertoire

`	ext
AudioScoreTranscriber/
├── audio_score_transcriber.png  # Vignette officielle épurée du plugin
├── setup_live_plugin.bat        # Configuration de la jonction NTFS native
├── requirements.txt             # Dépendances Python (DSP, ML, WebSockets)
├── .gitignore                   # Règles d'exclusion Git
├── README.md                    # Documentation d'architecture et d'utilisation
├── audioscore/                  # Package Python Sidecar
│   ├── __init__.py
│   ├── server.py                # Serveur WebSocket asynchrone
│   ├── audio_capture.py         # Capture micro basse latence & overdubbing
│   ├── engine_rhythm.py         # Moteur 1 : Détection percussive & transitoires
│   ├── engine_polyphonic.py     # Moteur 2 : Transcription polyphonique Basic Pitch
│   ├── engine_melodic.py        # Moteur 3 : Suivi de mélodie vocale / pYIN
│   └── quantizer.py             # Alignement sur grille métrique & MusicXML/MIDI
└── qml/                         # Interface MuseScore 4
    ├── AudioScoreTranscriber.qml# Composant principal du plugin MuseScore
    └── controls/                # Composants graphiques et widgets QML
`

---

## 📄 Licence & Crédits

- Conçu et développé par **Henri Jamet**.
- Licence **MIT**. Contributions bienvenues !
