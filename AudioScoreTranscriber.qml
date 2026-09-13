import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import MuseScore 3.0
import MuseScore.Playback 1.0
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

    width: 460
    height: 680

    // Palette système pour adaptation au thème MuseScore
    SystemPalette {
        id: sysPalette
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

    // WebSocket state
    property int wsClientId: -1
    property bool wsConnected: false
    property string wsStatus: "Déconnecté"
    property int wsPort: 8085

    // Playback state model
    property var playbackModel: null

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
    // Méthodes WebSocket (Communication Backend Python port 8085)
    // ========================================================================
    function connectWebSocket() {
        if (wsConnected) return;
        wsStatus = "Reconnexion...";

        try {
            if (typeof api !== "undefined" && api.websocket) {
                api.websocket.open(wsPort, function(sockId) {
                    console.log("[AudioScoreTranscriber] WebSocket connecté avec socketId : " + sockId);
                    wsClientId = sockId;
                    wsConnected = true;
                    wsStatus = "Connecté";

                    api.websocket.onMessage(sockId, function(rawMsg) {
                        handleBackendMessage(rawMsg);
                    });

                    // Handshake initial
                    sendWsJson({
                        command: "handshake",
                        client: "AudioScoreTranscriber_QML",
                        version: "1.0.0"
                    });
                });
                return;
            }
        } catch (err) {
            console.log("[AudioScoreTranscriber] api.websocket.open catch: " + err);
        }

        wsConnected = false;
        wsStatus = "Déconnecté";
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

            if (msg.type === "status") {
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
            } else if (msg.type === "vu_meter") {
                if (typeof msg.level === "number") {
                    vuMeterLevel = Math.max(0.0, Math.min(1.0, msg.level));
                }
            } else if (msg.type === "transcription_result") {
                appState = "ready";
                vuMeterLevel = 0.0;

                if (overdubCheck.checked && isRecording) {
                    stopMuseScorePlayback();
                }
                isRecording = false;

                injectTranscriptionResult(msg);
            } else if (msg.type === "error") {
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
        var events = result.events || [];
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
    Timer {
        id: wsReconnectTimer
        interval: 3000
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
        connectWebSocket();
    }

    // ========================================================================
    // Interface Graphique Épurée & Moderne (QtQuick Controls 2.15)
    // ========================================================================
    Rectangle {
        id: mainContainer
        anchors.fill: parent
        color: "#18181b" // Dark modern surface

        ScrollView {
            anchors.fill: parent
            contentWidth: parent.width
            clip: true

            ColumnLayout {
                width: mainContainer.width - 24
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 14

                Item { height: 6 } // Marge haute

                // ------------------------------------------------------------
                // 1. En-tête : Titre & Statut WebSocket
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    height: 56
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
                            height: 26
                            width: wsRow.implicitWidth + 16
                            radius: 13
                            color: pluginRoot.wsConnected ? "#064e3b" : (pluginRoot.wsStatus === "Reconnexion..." ? "#78350f" : "#450a0a")
                            border.color: pluginRoot.wsConnected ? "#10b981" : (pluginRoot.wsStatus === "Reconnexion..." ? "#f59e0b" : "#ef4444")
                            border.width: 1

                            RowLayout {
                                id: wsRow
                                anchors.centerIn: parent
                                spacing: 6

                                Rectangle {
                                    width: 8
                                    height: 8
                                    radius: 4
                                    color: pluginRoot.wsConnected ? "#10b981" : (pluginRoot.wsStatus === "Reconnexion..." ? "#f59e0b" : "#ef4444")
                                }

                                Text {
                                    text: pluginRoot.wsConnected ? "Port 8085 OK" : pluginRoot.wsStatus
                                    font.pixelSize: 11
                                    font.bold: true
                                    color: pluginRoot.wsConnected ? "#a7f3d0" : (pluginRoot.wsStatus === "Reconnexion..." ? "#fde68a" : "#fecaca")
                                }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    pluginRoot.connectWebSocket();
                                }
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 2. Sélecteur 3 Modes d'Enregistrement
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
                    height: 64
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
                                    height: 16
                                    width: badgeRhythmText.implicitWidth + 8
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
                    height: 64
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
                                    height: 16
                                    width: badgePianoText.implicitWidth + 8
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
                    height: 64
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
                                    height: 16
                                    width: badgeVocalText.implicitWidth + 8
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
                // 3. Curseur de Compensation de Latence RTL
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    height: 72
                    radius: 8
                    color: "#27272a"
                    border.color: "#3f3f46"
                    border.width: 1

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 4

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: "Compensation de latence aller-retour (RTL)"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#e4e4e7"
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: Math.round(latencySlider.value) + " ms"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#60a5fa"
                            }
                        }

                        Slider {
                            id: latencySlider
                            Layout.fillWidth: true
                            from: 0
                            to: 150
                            stepSize: 1
                            value: pluginRoot.rtlLatencyMs
                            onMoved: {
                                pluginRoot.rtlLatencyMs = Math.round(value);
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 4. Checkbox Overdubbing & Avertissement Casque
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: overdubCol.implicitHeight + 20
                    radius: 8
                    color: "#27272a"
                    border.color: overdubCheck.checked ? "#d97706" : "#3f3f46"
                    border.width: 1

                    ColumnLayout {
                        id: overdubCol
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 8

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            CheckBox {
                                id: overdubCheck
                                checked: true
                                text: "Overdubbing / Playback MuseScore synchrone"
                                font.pixelSize: 12
                                font.bold: true
                                contentItem: Text {
                                    text: overdubCheck.text
                                    font: overdubCheck.font
                                    color: "#fafafa"
                                    leftPadding: overdubCheck.indicator.width + 6
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }

                        // Avertissement Casque Audio Obligatoire
                        Rectangle {
                            Layout.fillWidth: true
                            height: 38
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
                // 5. Bouton Principal Enregistrer / Arrêter & Indicateur d'état
                // ------------------------------------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    height: 84
                    radius: 10
                    color: pluginRoot.isRecording ? "#7f1d1d" : "#1e293b"
                    border.color: pluginRoot.isRecording ? "#ef4444" : "#3b82f6"
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
                                width: 10
                                height: 10
                                radius: 5
                                color: pluginRoot.isRecording ? "#ef4444" : (pluginRoot.appState === "processing" ? "#f59e0b" : "#10b981")

                                SequentialAnimation on opacity {
                                    running: pluginRoot.isRecording || pluginRoot.appState === "processing"
                                    loops: Animation.Infinite
                                    PropertyAnimation { to: 0.2; duration: 400 }
                                    PropertyAnimation { to: 1.0; duration: 400 }
                                }
                            }

                            Text {
                                text: pluginRoot.statusText
                                font.pixelSize: 12
                                font.bold: true
                                color: "#f4f4f5"
                            }

                            Item { Layout.fillWidth: true }

                            // VU-Mètre horizontal
                            Rectangle {
                                width: 90
                                height: 8
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

                        // Bouton principal d'action
                        Button {
                            id: mainActionButton
                            Layout.fillWidth: true
                            Layout.fillHeight: true

                            contentItem: Text {
                                text: pluginRoot.isRecording ? "⏹️  ARRÊTER & TRANSCRIRE" :
                                      (pluginRoot.appState === "processing" ? "⚙️  TRAITEMENT DSP EN COURS..." : "🔴  ENREGISTRER")
                                font.pixelSize: 14
                                font.bold: true
                                color: "#ffffff"
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }

                            background: Rectangle {
                                radius: 6
                                color: pluginRoot.isRecording ? "#dc2626" :
                                       (pluginRoot.appState === "processing" ? "#d97706" : "#2563eb")
                            }

                            cursorShape: Qt.PointingHandCursor
                            enabled: pluginRoot.appState !== "processing"
                            onClicked: {
                                pluginRoot.toggleRecording();
                            }
                        }
                    }
                }

                // ------------------------------------------------------------
                // 6. Message de Feedback & Résultats
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

                // ------------------------------------------------------------
                // 7. Barre d'outils de test direct (Diagnostic in-situ)
                // ------------------------------------------------------------
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    Layout.topMargin: 4

                    Text {
                        text: "Test direct partition :"
                        font.pixelSize: 10
                        color: "#71717a"
                    }

                    Item { Layout.fillWidth: true }

                    Button {
                        text: "Test 🥁 Rythme"
                        font.pixelSize: 10
                        implicitHeight: 24
                        onClicked: {
                            var r = ScoreCursorWriter.testInjection("rhythm", curScore);
                            pluginRoot.feedbackText = r.success ? "✅ Test Rythme injecté (" + r.count + " notes)" : "❌ " + r.error;
                        }
                    }

                    Button {
                        text: "Test 🎹 Piano"
                        font.pixelSize: 10
                        implicitHeight: 24
                        onClicked: {
                            var r = ScoreCursorWriter.testInjection("piano", curScore);
                            pluginRoot.feedbackText = r.success ? "✅ Test Piano injecté (" + r.count + " notes)" : "❌ " + r.error;
                        }
                    }

                    Button {
                        text: "Test 🎤 Chant"
                        font.pixelSize: 10
                        implicitHeight: 24
                        onClicked: {
                            var r = ScoreCursorWriter.testInjection("vocal", curScore);
                            pluginRoot.feedbackText = r.success ? "✅ Test Chant injecté (" + r.count + " notes)" : "❌ " + r.error;
                        }
                    }
                }

                Item { height: 12 } // Marge basse
            }
        }
    }
}
