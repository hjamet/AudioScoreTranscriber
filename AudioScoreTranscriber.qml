import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import Muse.UiComponents
import MuseScore.Playback
import MuseScore
import "ScoreCursorWriter.js" as ScoreCursorWriter

MuseScore {
    id: pluginRoot

    // Métadonnées officielles MuseScore 4
    menuPath: "Plugins.Audio Score Transcriber"
    title: "Audio Score Transcriber"
    description: "Transcription audio vers partition directe dans MuseScore 4 (Rythme, Piano polyphonique, Chant mélodique)."
    version: "1.0.0"
    thumbnailName: "audio_score_transcriber.png"
    categoryCode: "composing-arranging-tools"
    pluginType: "dialog"
    requiresScore: true

    implicitWidth: 460
    implicitHeight: 630
    width: 460
    height: 630

    // Palette système pour adaptation au thème MuseScore
    SystemPalette {
        id: sysPalette
    }

    QProcess {
        id: qproc
    }

    // ========================================================================
    // Propriétés d'état du Plugin
    // ========================================================================
    property string selectedMode: "rhythm"  // "rhythm" | "piano" | "vocal"
    property int rtlLatencyMs: 35
    property bool isRecording: false
    property string appState: "ready"       // "ready" | "countdown" | "recording" | "processing"
    property string statusText: "Prêt"
    property string feedbackText: ""
    property real vuMeterLevel: 0.0

    // État du sélecteur d'entrée audio (Microphone)
    property var audioDevices: []
    property int selectedDeviceIndex: -1
    property string selectedDeviceName: "Recherche des micros..."
    property bool micDropdownOpen: false

    // WebSocket state
    property int wsClientId: -1
    property bool wsConnected: false
    property string wsStatus: "Déconnecté"
    property int wsPort: 8085
    property bool backendStarting: false
    property bool hasSelection: false

    // Playback state model
    property var playbackModel: null

    function normalizePath(path) {
        var p = path.toString();
        var normalizedPath;
        if (p.startsWith('file://')) {
            normalizedPath = p.replace(/^file:\/\/\/?/, '');
            if (Qt.platform.os !== "windows" && !normalizedPath.startsWith('/')) {
                normalizedPath = "/" + normalizedPath;
            }
        } else {
            normalizedPath = p;
        }
        return normalizedPath;
    }

    function startBackend() {
        if (backendStarting || wsConnected) return;
        backendStarting = true;
        console.log("[AudioScoreTranscriber] Auto-lancement du backend Python via QProcess...");
        var pluginDir = normalizePath(Qt.resolvedUrl("."));
        if (!pluginDir.endsWith("/")) pluginDir += "/";
        var batPath = pluginDir + "run_backend.bat";
        var pyPath = pluginDir + "backend/main.py";

        try {
            if (Qt.platform.os === "windows") {
                qproc.startWithArgs("cmd.exe", ["/c", batPath]);
            } else {
                qproc.startWithArgs("python3", [pyPath, "--port", "8085"]);
            }
        } catch (e) {
            console.log("[AudioScoreTranscriber] Exception démarrage QProcess: " + e);
            try {
                qproc.startWithArgs("python", [pyPath, "--port", "8085"]);
            } catch (e2) {
                console.log("[AudioScoreTranscriber] Exception fallback python: " + e2);
            }
        }
    }

    function checkSelection() {
        if (!curScore) return false;
        try {
            if (curScore.selection && curScore.selection.elements && curScore.selection.elements.length > 0) {
                return true;
            }
            var c = curScore.newCursor();
            if (c) {
                c.rewind(1); // 1 = Cursor.SELECTION_START
                if (c.segment || c.element) {
                    return true;
                }
            }
        } catch (e) {
            // Silencieux
        }
        return false;
    }

    PlaybackToolBarModel {
        id: directPlaybackModel
        Component.onCompleted: {
            try {
                this.load();
                pluginRoot.playbackModel = this;
            } catch (e) {
                console.log("[AudioScoreTranscriber] PlaybackToolBarModel notice: " + e);
            }
        }
    }

    // ========================================================================
    // Méthodes de Playback MuseScore (Overdubbing)
    // ========================================================================
    function startMuseScorePlayback() {
        try {
            cmd("command://playback/play");
        } catch (e1) {
            try {
                cmd("play");
            } catch (e2) {
                console.log("[AudioScoreTranscriber] startMuseScorePlayback notice: " + e2);
            }
        }
    }

    function stopMuseScorePlayback() {
        try {
            cmd("command://playback/play");
        } catch (e1) {
            try {
                cmd("stop");
            } catch (e2) {
                console.log("[AudioScoreTranscriber] stopMuseScorePlayback notice: " + e2);
            }
        }
    }

    // ========================================================================
    // Méthodes de Gestion du Périphérique d'Entrée Audio
    // ========================================================================
    function selectDevice(deviceIndex, deviceName) {
        pluginRoot.selectedDeviceIndex = deviceIndex;
        pluginRoot.selectedDeviceName = deviceName;
        console.log("[AudioScoreTranscriber] Périphérique micro sélectionné : #" + deviceIndex + " (" + deviceName + ")");
        sendWsJson({
            command: "set_device",
            device_index: deviceIndex
        });
    }

    function refreshDevices() {
        console.log("[AudioScoreTranscriber] Actualisation de la liste des périphériques audio...");
        if (wsConnected) {
            sendWsJson({
                command: "get_devices"
            });
        }
    }

    // ========================================================================
    // Méthodes WebSocket (Communication Backend Python port 8085)
    // ========================================================================
    function connectWebSocket() {
        if (wsConnected) return;
        wsStatus = "Connexion...";

        try {
            if (typeof api !== "undefined" && api.websocket) {
                api.websocket.open(wsPort, function(sockId) {
                    console.log("[AudioScoreTranscriber] WebSocket connecté avec socketId : " + sockId);
                    wsClientId = sockId;
                    wsConnected = true;
                    backendStarting = false;
                    wsStatus = "Connecté";
                    if (statusText === "Connexion au moteur..." || statusText === "Moteur non connecté") {
                        statusText = "Prêt";
                    }

                    api.websocket.onMessage(sockId, function(rawMsg) {
                        handleBackendMessage(rawMsg);
                    });

                    // Handshake initial & requête de la liste des micros
                    sendWsJson({
                        command: "handshake",
                        client: "AudioScoreTranscriber_QML",
                        version: "1.0.0"
                    });
                    sendWsJson({
                        command: "get_devices"
                    });
                });
                return;
            }
        } catch (err) {
            console.log("[AudioScoreTranscriber] api.websocket.open catch: " + err);
        }

        wsConnected = false;
        wsStatus = "Déconnecté";
        if (!backendStarting) {
            startBackend();
        }
    }

    function sendWsJson(dataObj) {
        var str = JSON.stringify(dataObj);
        if (wsConnected && wsClientId !== -1 && typeof api !== "undefined" && api.websocket) {
            try {
                api.websocket.send(wsClientId, str);
                return true;
            } catch (e) {
                console.log("[AudioScoreTranscriber] Erreur envoi WebSocket: " + e);
                wsConnected = false;
                wsStatus = "Déconnecté";
                return false;
            }
        }
        return false;
    }

    function handleBackendMessage(rawMsg) {
        try {
            var msg = JSON.parse(rawMsg);
            if (!msg) return;

            var eventType = (msg.type || msg.event || "").toLowerCase();

            if (eventType === "status" || eventType === "handshake_ok") {
                if (msg.state === "countdown") {
                    appState = "countdown";
                    statusText = "Décompte : " + (msg.count !== undefined ? msg.count : "...");
                } else if (msg.state === "recording") {
                    appState = "recording";
                    var sec = (typeof msg.duration_sec === "number") ? msg.duration_sec.toFixed(1) + "s" : "";
                    statusText = "Enregistrement en cours " + sec;
                    if (typeof msg.vu_level === "number") {
                        vuMeterLevel = Math.max(0.0, Math.min(1.0, msg.vu_level));
                    }
                } else if (msg.state === "processing") {
                    appState = "processing";
                    statusText = "Traitement DSP & Inférence...";
                } else if (msg.state === "ready") {
                    appState = "ready";
                    statusText = "Prêt";
                }
            } else if (eventType === "vu_meter") {
                if (typeof msg.level === "number") {
                    vuMeterLevel = Math.max(0.0, Math.min(1.0, msg.level));
                }
            } else if (eventType === "recording_started") {
                appState = "recording";
                statusText = "Enregistrement en cours...";
            } else if (eventType === "recording_stopped") {
                appState = "processing";
                statusText = "Traitement audio & Inférence...";
            } else if (eventType === "devices_list") {
                if (msg.devices && Array.isArray(msg.devices)) {
                    pluginRoot.audioDevices = msg.devices;
                    var found = false;
                    var targetIdx = (typeof msg.current_device === "number" && msg.current_device >= 0) ? msg.current_device : -1;

                    // Conserver la sélection existante si toujours présente
                    if (pluginRoot.selectedDeviceIndex >= 0) {
                        for (var i = 0; i < msg.devices.length; i++) {
                            if (msg.devices[i].index === pluginRoot.selectedDeviceIndex) {
                                pluginRoot.selectedDeviceName = msg.devices[i].display_name || msg.devices[i].name;
                                found = true;
                                break;
                            }
                        }
                    }

                    // Sinon appliquer le périphérique courant du backend
                    if (!found && targetIdx >= 0) {
                        for (var j = 0; j < msg.devices.length; j++) {
                            if (msg.devices[j].index === targetIdx) {
                                pluginRoot.selectedDeviceIndex = targetIdx;
                                pluginRoot.selectedDeviceName = msg.devices[j].display_name || msg.devices[j].name;
                                found = true;
                                break;
                            }
                        }
                    }

                    // Repli intelligent : recherche de l'HyperX QuadCast ou du périphérique par défaut
                    if (!found && msg.devices.length > 0) {
                        var chosenDev = msg.devices[0];
                        for (var k = 0; k < msg.devices.length; k++) {
                            var dName = (msg.devices[k].name || "").toLowerCase();
                            if (dName.indexOf("hyperx") !== -1 || dName.indexOf("quadcast") !== -1) {
                                chosenDev = msg.devices[k];
                                break;
                            } else if (msg.devices[k].is_default) {
                                chosenDev = msg.devices[k];
                            }
                        }
                        pluginRoot.selectedDeviceIndex = chosenDev.index;
                        pluginRoot.selectedDeviceName = chosenDev.display_name || chosenDev.name;
                    }
                    console.log("[AudioScoreTranscriber] Micros disponibles : " + msg.devices.length + ", actif : #" + pluginRoot.selectedDeviceIndex + " (" + pluginRoot.selectedDeviceName + ")");
                }
            } else if (eventType === "device_set") {
                if (typeof msg.current_device === "number") {
                    pluginRoot.selectedDeviceIndex = msg.current_device;
                    for (var m = 0; m < pluginRoot.audioDevices.length; m++) {
                        if (pluginRoot.audioDevices[m].index === msg.current_device) {
                            pluginRoot.selectedDeviceName = pluginRoot.audioDevices[m].display_name || pluginRoot.audioDevices[m].name;
                            break;
                        }
                    }
                }
            } else if (eventType === "transcription_result") {
                dspTimeoutTimer.stop();
                appState = "ready";
                vuMeterLevel = 0.0;

                if (overdubCheck.checked && isRecording) {
                    stopMuseScorePlayback();
                }
                isRecording = false;

                injectTranscriptionResult(msg);
            } else if (eventType === "error") {
                dspTimeoutTimer.stop();
                statusText = "Erreur : " + (msg.message || "inconnue");
                feedbackText = "❌ " + (msg.message || "Erreur backend");
                appState = "ready";
                isRecording = false;
                if (overdubCheck.checked) {
                    stopMuseScorePlayback();
                }
            }
        } catch (e) {
            console.log("[AudioScoreTranscriber] Exception JSON backend message: " + e);
        }
    }

    // ========================================================================
    // Injection dans le DOM MuseScore via ScoreCursorWriter.js
    // ========================================================================
    function injectTranscriptionResult(result) {
        statusText = "Injection dans la partition...";
        var targetMode = result.mode || selectedMode;
        var events = result.events || result.data || [];
        var res = null;

        if (targetMode === "rhythm") {
            res = ScoreCursorWriter.writeRhythmEvents(events, 60, 0, 0, curScore);
        } else if (targetMode === "piano") {
            res = ScoreCursorWriter.writePianoEvents(events, null, curScore);
        } else if (targetMode === "vocal") {
            res = ScoreCursorWriter.writeVocalEvents(events, 0, 0, curScore);
        }

        if (res && res.success) {
            feedbackText = "✅ " + (res.count || 0) + " éléments injectés avec succès (" + targetMode + ") !";
            statusText = "Prêt";
        } else {
            var errMsg = res ? (res.error || "Erreur inconnue") : "Échec d'injection";
            feedbackText = "❌ Échec injection : " + errMsg;
            statusText = "Erreur d'injection";
        }
    }

    // ========================================================================
    // Gestion du cycle d'enregistrement
    // ========================================================================
    function toggleRecording() {
        if (!wsConnected) {
            feedbackText = "⚠️ Connexion au moteur Python en cours...";
            connectWebSocket();
            return;
        }

        if (!hasSelection && !isRecording) {
            feedbackText = "⚠️ Veuillez sélectionner une mesure ou une portée dans la partition.";
            return;
        }

        if (!isRecording) {
            // Démarrage
            isRecording = true;
            appState = "recording";
            statusText = "Enregistrement en cours...";
            feedbackText = "";
            vuMeterLevel = 0.15;

            // Déclenchement Playback synchrone si overdubbing
            if (overdubCheck.checked) {
                startMuseScorePlayback();
            }

            // Notification WebSocket
            sendWsJson({
                command: "start_recording",
                mode: selectedMode,
                rtl_latency_ms: rtlLatencyMs,
                device_index: pluginRoot.selectedDeviceIndex >= 0 ? pluginRoot.selectedDeviceIndex : undefined,
                overdub: overdubCheck.checked
            });
        } else {
            // Arrêt
            isRecording = false;
            appState = "processing";
            statusText = "Traitement audio & Inférence...";
            vuMeterLevel = 0.0;

            if (overdubCheck.checked) {
                stopMuseScorePlayback();
            }

            // Armement du timeout de sécurité DSP (8 secondes)
            dspTimeoutTimer.restart();

            sendWsJson({
                command: "stop_recording",
                mode: selectedMode,
                rtl_latency_ms: rtlLatencyMs
            });
        }
    }

    // ========================================================================
    // Timers de supervision & Reconnexion
    // ========================================================================

    // Timeout de sécurité DSP (8 secondes max)
    Timer {
        id: dspTimeoutTimer
        interval: 8000
        repeat: false
        running: false
        onTriggered: {
            console.log("[AudioScoreTranscriber] Timeout DSP atteint (8s).");
            pluginRoot.appState = "ready";
            pluginRoot.isRecording = false;
            pluginRoot.vuMeterLevel = 0.0;
            pluginRoot.statusText = "Prêt";
            pluginRoot.feedbackText = "❌ Délai d'attente dépassé (aucune réponse DSP en 8s)";
            if (overdubCheck.checked) {
                pluginRoot.stopMuseScorePlayback();
            }
        }
    }

    // Surveillance continue de la sélection active dans MuseScore
    Timer {
        id: selectionCheckerTimer
        interval: 300
        running: true
        repeat: true
        onTriggered: {
            pluginRoot.hasSelection = pluginRoot.checkSelection();
        }
    }

    Timer {
        id: wsReconnectTimer
        interval: 2000
        running: !pluginRoot.wsConnected
        repeat: true
        onTriggered: {
            pluginRoot.connectWebSocket();
        }
    }

    // Animation légère du VU-mètre quand recording actif sans backend réel
    Timer {
        id: vuSimTimer
        interval: 120
        running: pluginRoot.isRecording && !pluginRoot.wsConnected
        repeat: true
        onTriggered: {
            pluginRoot.vuMeterLevel = 0.2 + (Math.random() * 0.6);
        }
    }

    onRun: {
        console.log("[AudioScoreTranscriber] Démarrage du plugin");
        hasSelection = checkSelection();
        startBackend();
        connectWebSocket();
    }

    // ========================================================================
    // Interface Graphique Épurée & Moderne (100% Compatible Qt 6 / MuseScore 4)
    // ========================================================================
    Rectangle {
        id: mainContainer
        anchors.fill: parent
        color: "#18181b" // Dark modern surface

        Flickable {
            id: scrollArea
            anchors.fill: parent
            contentWidth: width
            contentHeight: contentCol.implicitHeight + 36
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            ColumnLayout {
                id: contentCol
                width: mainContainer.width - 24
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 14

                Item { Layout.preferredHeight: 6; Layout.fillWidth: true } // Marge haute

                // ------------------------------------------------------------
                // 1. En-tête : Titre & Statut WebSocket
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    radius: 8
                    color: "#27272a"
                    border.color: "#3f3f46"
                    border.width: 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 10

                        Text {
                            text: "🎼 AudioScore Transcriber"
                            font.pixelSize: 15
                            font.bold: true
                            color: "#fafafa"
                        }

                        Item { Layout.fillWidth: true }

                        // Badge de statut WebSocket
                        Rectangle {
                            Layout.preferredHeight: 26
                            Layout.preferredWidth: wsRow.implicitWidth + 16
                            radius: 13
                            color: pluginRoot.wsConnected ? "#064e3b" : (pluginRoot.wsStatus === "Connexion..." || pluginRoot.backendStarting ? "#78350f" : "#450a0a")
                            border.color: pluginRoot.wsConnected ? "#10b981" : (pluginRoot.wsStatus === "Connexion..." || pluginRoot.backendStarting ? "#f59e0b" : "#ef4444")
                            border.width: 1

                            RowLayout {
                                id: wsRow
                                anchors.centerIn: parent
                                spacing: 6

                                Rectangle {
                                    Layout.preferredWidth: 8
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: pluginRoot.wsConnected ? "#10b981" : (pluginRoot.wsStatus === "Connexion..." || pluginRoot.backendStarting ? "#f59e0b" : "#ef4444")
                                }

                                Text {
                                    text: pluginRoot.wsConnected ? "Moteur Prêt (8085)" : (pluginRoot.backendStarting ? "Démarrage moteur..." : pluginRoot.wsStatus)
                                    font.pixelSize: 11
                                    font.bold: true
                                    color: pluginRoot.wsConnected ? "#a7f3d0" : (pluginRoot.wsStatus === "Connexion..." || pluginRoot.backendStarting ? "#fde68a" : "#fecaca")
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    pluginRoot.startBackend();
                                    pluginRoot.connectWebSocket();
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 2. Sélecteur de Périphérique Audio (Microphone)
                // ------------------------------------------------------------
                Text {
                    text: "ENTRÉE VOCALE (MICROPHONE)"
                    font.pixelSize: 11
                    font.bold: true
                    color: "#a1a1aa"
                    Layout.topMargin: 2
                }

                Rectangle {
                    id: micSelectorCard
                    Layout.fillWidth: true
                    Layout.preferredHeight: pluginRoot.micDropdownOpen ? Math.min(260, 52 + (pluginRoot.audioDevices.length * 36) + 12) : 52
                    radius: 8
                    color: "#27272a"
                    border.color: pluginRoot.micDropdownOpen ? "#60a5fa" : "#3f3f46"
                    border.width: pluginRoot.micDropdownOpen ? 2 : 1
                    clip: true

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        // Barre principale du sélecteur
                        RowLayout {
                            id: micHeaderRow
                            Layout.fillWidth: true
                            Layout.preferredHeight: 36
                            spacing: 10

                            // Clic sur l'icône et le nom pour déplier/replier
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 10

                                Text {
                                    text: "🎙️"
                                    font.pixelSize: 20
                                    Layout.leftMargin: 4
                                }

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1

                                    Text {
                                        text: "Microphone sélectionné"
                                        font.pixelSize: 10
                                        color: "#a1a1aa"
                                    }

                                    Text {
                                        text: pluginRoot.selectedDeviceName
                                        font.pixelSize: 12
                                        font.bold: true
                                        color: "#fafafa"
                                        elide: Text.ElideRight
                                        Layout.fillWidth: true
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        pluginRoot.micDropdownOpen = !pluginRoot.micDropdownOpen;
                                    }
                                }
                            }

                            // Bouton d'actualisation discret
                            Rectangle {
                                Layout.preferredWidth: 28
                                Layout.preferredHeight: 28
                                radius: 4
                                color: refreshMouseArea.containsMouse ? "#3f3f46" : "#1e1e24"
                                border.color: "#3f3f46"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "🔄"
                                    font.pixelSize: 12
                                }

                                MouseArea {
                                    id: refreshMouseArea
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        pluginRoot.refreshDevices();
                                    }
                                }
                            }

                            // Chevron indicateur déroulant
                            Rectangle {
                                Layout.preferredWidth: 28
                                Layout.preferredHeight: 28
                                radius: 4
                                color: chevronMouseArea.containsMouse ? "#3f3f46" : "#1e1e24"
                                border.color: "#3f3f46"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: pluginRoot.micDropdownOpen ? "▲" : "▼"
                                    font.pixelSize: 10
                                    font.bold: true
                                    color: "#fafafa"
                                }

                                MouseArea {
                                    id: chevronMouseArea
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        pluginRoot.micDropdownOpen = !pluginRoot.micDropdownOpen;
                                    }
                                }
                            }
                        }

                        // Séparateur fin
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            color: "#3f3f46"
                            visible: pluginRoot.micDropdownOpen
                        }

                        // Liste déroulante des périphériques
                        Flickable {
                            id: micListFlickable
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            contentWidth: width
                            contentHeight: micItemsCol.implicitHeight
                            clip: true
                            visible: pluginRoot.micDropdownOpen
                            boundsBehavior: Flickable.StopAtBounds

                            ColumnLayout {
                                id: micItemsCol
                                width: micListFlickable.width
                                spacing: 4

                                Repeater {
                                    model: pluginRoot.audioDevices

                                    Rectangle {
                                        id: devItemRect
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 32
                                        radius: 6
                                        property bool isSelected: modelData.index === pluginRoot.selectedDeviceIndex
                                        color: isSelected ? "#1e3a8a" : (itemMouseArea.containsMouse ? "#3f3f46" : "#18181b")
                                        border.color: isSelected ? "#60a5fa" : "transparent"
                                        border.width: 1

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 8
                                            anchors.rightMargin: 8
                                            spacing: 6

                                            Text {
                                                text: devItemRect.isSelected ? "✓" : "•"
                                                font.pixelSize: 11
                                                font.bold: true
                                                color: devItemRect.isSelected ? "#60a5fa" : "#71717a"
                                            }

                                            Text {
                                                text: modelData.display_name || modelData.name
                                                font.pixelSize: 11
                                                font.bold: devItemRect.isSelected
                                                color: devItemRect.isSelected ? "#ffffff" : "#e4e4e7"
                                                elide: Text.ElideRight
                                                Layout.fillWidth: true
                                            }

                                            Text {
                                                text: modelData.default_samplerate ? (modelData.default_samplerate + " Hz") : ""
                                                font.pixelSize: 9
                                                color: devItemRect.isSelected ? "#93c5fd" : "#71717a"
                                            }
                                        }

                                        MouseArea {
                                            id: itemMouseArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                pluginRoot.selectDevice(modelData.index, modelData.display_name || modelData.name);
                                                pluginRoot.micDropdownOpen = false;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 3. Sélecteur 3 Modes d'Enregistrement
                // ------------------------------------------------------------
                Text {
                    text: "MODE DE TRANSCRIPTION"
                    font.pixelSize: 11
                    font.bold: true
                    color: "#a1a1aa"
                    Layout.topMargin: 4
                }

                // Carte Mode 1 : Rythme Seul
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 64
                    radius: 8
                    color: pluginRoot.selectedMode === "rhythm" ? "#1e3a8a" : "#27272a"
                    border.color: pluginRoot.selectedMode === "rhythm" ? "#60a5fa" : "#3f3f46"
                    border.width: pluginRoot.selectedMode === "rhythm" ? 2 : 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 12

                        Text {
                            text: "🥁"
                            font.pixelSize: 24
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: "Rythme Seul"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: "#fafafa"
                                }
                                Rectangle {
                                    Layout.preferredHeight: 18
                                    Layout.preferredWidth: badgeRhythmText.implicitWidth + 10
                                    radius: 4
                                    color: "#1e293b"
                                    Text {
                                        id: badgeRhythmText
                                        anchors.centerIn: parent
                                        text: "Sans pitch / Note fixe"
                                        font.pixelSize: 9
                                        color: "#94a3b8"
                                    }
                                }
                            }

                            Text {
                                text: "Table, tapotement, onomatopées \"tac-tac\" -> C4 ou note active"
                                font.pixelSize: 11
                                color: "#cbd5e1"
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            pluginRoot.selectedMode = "rhythm";
                        }
                    }
                }

                // Carte Mode 2 : Piano Polyphonique
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 64
                    radius: 8
                    color: pluginRoot.selectedMode === "piano" ? "#1e3a8a" : "#27272a"
                    border.color: pluginRoot.selectedMode === "piano" ? "#60a5fa" : "#3f3f46"
                    border.width: pluginRoot.selectedMode === "piano" ? 2 : 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 12

                        Text {
                            text: "🎹"
                            font.pixelSize: 24
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: "Piano Polyphonique"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: "#fafafa"
                                }
                                Rectangle {
                                    Layout.preferredHeight: 18
                                    Layout.preferredWidth: badgePianoText.implicitWidth + 10
                                    radius: 4
                                    color: "#1e293b"
                                    Text {
                                        id: badgePianoText
                                        anchors.centerIn: parent
                                        text: "Grand Staff (Sol/Fa)"
                                        font.pixelSize: 9
                                        color: "#94a3b8"
                                    }
                                }
                            }

                            Text {
                                text: "ByteDance High-Res, accords plaqués, arpèges, pédale"
                                font.pixelSize: 11
                                color: "#cbd5e1"
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            pluginRoot.selectedMode = "piano";
                        }
                    }
                }

                // Carte Mode 3 : Chant Mélodique
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 64
                    radius: 8
                    color: pluginRoot.selectedMode === "vocal" ? "#1e3a8a" : "#27272a"
                    border.color: pluginRoot.selectedMode === "vocal" ? "#60a5fa" : "#3f3f46"
                    border.width: pluginRoot.selectedMode === "vocal" ? 2 : 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 12

                        Text {
                            text: "🎤"
                            font.pixelSize: 24
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: "Chant Mélodique"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: "#fafafa"
                                }
                                Rectangle {
                                    Layout.preferredHeight: 18
                                    Layout.preferredWidth: badgeVocalText.implicitWidth + 10
                                    radius: 4
                                    color: "#1e293b"
                                    Text {
                                        id: badgeVocalText
                                        anchors.centerIn: parent
                                        text: "Monophonie & Liaisons"
                                        font.pixelSize: 9
                                        color: "#94a3b8"
                                    }
                                }
                            }

                            Text {
                                text: "Modèle RMVPE / pYIN, anti-vibrato 220 ms, liaisons auto"
                                font.pixelSize: 11
                                color: "#cbd5e1"
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            pluginRoot.selectedMode = "vocal";
                        }
                    }
                }

                // ------------------------------------------------------------
                // 4. Avertissement Sélection de Mesure Requise
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 38
                    radius: 6
                    color: "#451a03"
                    border.color: "#f59e0b"
                    border.width: 1
                    visible: !pluginRoot.hasSelection

                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 8

                        Text {
                            text: "⚠️"
                            font.pixelSize: 14
                        }

                        Text {
                            text: "Veuillez sélectionner une mesure ou une portée dans la partition"
                            font.pixelSize: 11
                            font.bold: true
                            color: "#fef3c7"
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }
                }

                // ------------------------------------------------------------
                // 5. Checkbox Overdubbing & Avertissement Casque
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: overdubCol.implicitHeight + 20
                    radius: 8
                    color: "#27272a"
                    border.color: overdubCheck.checked ? "#d97706" : "#3f3f46"
                    border.width: 1

                    ColumnLayout {
                        id: overdubCol
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 8

                        // Ligne Checkbox personnalisée (100% robuste, zéro dépendance controls)
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                id: overdubCheck
                                property bool checked: true
                                Layout.preferredWidth: 20
                                Layout.preferredHeight: 20
                                radius: 4
                                color: checked ? "#2563eb" : "#3f3f46"
                                border.color: checked ? "#60a5fa" : "#71717a"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "✓"
                                    font.pixelSize: 13
                                    font.bold: true
                                    color: "#ffffff"
                                    visible: overdubCheck.checked
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: overdubCheck.checked = !overdubCheck.checked
                                }
                            }

                            Text {
                                text: "Overdubbing / Playback MuseScore synchrone"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#fafafa"
                                Layout.fillWidth: true

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: overdubCheck.checked = !overdubCheck.checked
                                }
                            }
                        }

                        // Avertissement Casque Audio Obligatoire
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 38
                            radius: 6
                            color: "#451a03"
                            border.color: "#f59e0b"
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 8

                                Text {
                                    text: "🎧"
                                    font.pixelSize: 16
                                }

                                Text {
                                    text: "Casque audio obligatoire pour l'overdubbing (isolation acoustique passive)."
                                    font.pixelSize: 10
                                    font.bold: true
                                    color: "#fef3c7"
                                    wrapMode: Text.WordWrap
                                    Layout.fillWidth: true
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 6. Bouton Principal Enregistrer / Arrêter & Indicateur d'état
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 90
                    radius: 10
                    color: !pluginRoot.wsConnected ? "#27272a" :
                           (!pluginRoot.hasSelection && !pluginRoot.isRecording ? "#27272a" :
                           (pluginRoot.isRecording ? "#7f1d1d" : "#1e293b"))
                    border.color: !pluginRoot.wsConnected ? "#52525b" :
                                  (!pluginRoot.hasSelection && !pluginRoot.isRecording ? "#d97706" :
                                  (pluginRoot.isRecording ? "#ef4444" : "#3b82f6"))
                    border.width: 2

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        // Indicateur d'état dynamique & VU-mètre
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                Layout.preferredWidth: 10
                                Layout.preferredHeight: 10
                                radius: 5
                                color: !pluginRoot.wsConnected ? "#71717a" :
                                       (pluginRoot.isRecording ? "#ef4444" :
                                       (pluginRoot.appState === "processing" ? "#f59e0b" : "#10b981"))

                                SequentialAnimation on opacity {
                                    running: pluginRoot.isRecording || pluginRoot.appState === "processing"
                                    loops: Animation.Infinite
                                    PropertyAnimation { to: 0.2; duration: 400 }
                                    PropertyAnimation { to: 1.0; duration: 400 }
                                }
                            }

                            Text {
                                text: !pluginRoot.wsConnected ? "En attente du moteur Python..." :
                                      (!pluginRoot.hasSelection && !pluginRoot.isRecording ? "Sélection requise" :
                                      pluginRoot.statusText)
                                font.pixelSize: 12
                                font.bold: true
                                color: "#f4f4f5"
                            }

                            Item { Layout.fillWidth: true }

                            // VU-Mètre horizontal
                            Rectangle {
                                Layout.preferredWidth: 90
                                Layout.preferredHeight: 8
                                radius: 4
                                color: "#334155"
                                clip: true

                                Rectangle {
                                    width: parent.width * pluginRoot.vuMeterLevel
                                    height: parent.height
                                    radius: 4
                                    color: pluginRoot.vuMeterLevel > 0.8 ? "#ef4444" : (pluginRoot.vuMeterLevel > 0.5 ? "#f59e0b" : "#10b981")
                                }
                            }
                        }

                        // Bouton principal d'action personnalisé (100% robuste & interactif)
                        Rectangle {
                            id: mainActionButton
                            Layout.fillWidth: true
                            Layout.preferredHeight: 44
                            radius: 6
                            property bool canClick: pluginRoot.wsConnected &&
                                                   (pluginRoot.appState !== "processing") &&
                                                   (pluginRoot.isRecording || pluginRoot.hasSelection)
                            color: !pluginRoot.wsConnected ? "#3f3f46" :
                                   (pluginRoot.appState === "processing" ? "#d97706" :
                                   (pluginRoot.isRecording ? "#dc2626" :
                                   (!pluginRoot.hasSelection ? "#3f3f46" :
                                   (actionMouseArea.pressed ? "#1d4ed8" : "#2563eb"))))
                            opacity: canClick ? 1.0 : 0.65
                            border.color: !pluginRoot.wsConnected ? "#52525b" :
                                          (pluginRoot.isRecording ? "#fca5a5" :
                                          (pluginRoot.appState === "processing" ? "#fcd34d" :
                                          (!pluginRoot.hasSelection ? "#71717a" : "#93c5fd")))
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: !pluginRoot.wsConnected ? "⏳  CONNEXION AU MOTEUR..." :
                                      (pluginRoot.appState === "processing" ? "⚙️  TRAITEMENT DSP EN COURS..." :
                                      (pluginRoot.isRecording ? "⏹️  ARRÊTER & TRANSCRIRE" :
                                      (!pluginRoot.hasSelection ? "⚠️  SÉLECTIONNER UNE MESURE D'ABORD" : "🔴  ENREGISTRER")))
                                font.pixelSize: 13
                                font.bold: true
                                color: "#ffffff"
                            }

                            MouseArea {
                                id: actionMouseArea
                                anchors.fill: parent
                                cursorShape: mainActionButton.canClick ? Qt.PointingHandCursor : Qt.ArrowCursor
                                enabled: mainActionButton.canClick
                                onClicked: {
                                    pluginRoot.toggleRecording();
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 7. Message de Feedback & Résultats
                // ------------------------------------------------------------
                Text {
                    id: feedbackLabel
                    text: pluginRoot.feedbackText
                    font.pixelSize: 11
                    font.bold: true
                    color: pluginRoot.feedbackText.startsWith("✅") ? "#34d399" : "#f87171"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                    visible: pluginRoot.feedbackText !== ""
                }

                Item { Layout.preferredHeight: 12; Layout.fillWidth: true } // Marge basse
            }
        }
    }
}
