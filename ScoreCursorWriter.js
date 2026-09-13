/**
 * ScoreCursorWriter.js - AudioScoreTranscriber Cursor Injection Engine
 * 
 * Manipulation directe du DOM de partition MuseScore 4 via l'API Cursor (curScore.newCursor()).
 * Encadrement transactionnel strict (startCmd / endCmd) pour chaque injection.
 * 
 * Supports :
 * 1. writeRhythmEvents  : Injection d'attaques percussives quantifiées et silences.
 * 2. writePianoEvents   : Accords polyphoniques (addToChord = true) et Grand Staff (Sol/Fa).
 * 3. writeVocalEvents   : Ligne mélodique monophonique avec liaisons (cmd("tie")) aux barres de mesure.
 */

// Constantes métriques MuseScore standard (480 ticks par noire, 1920 ticks par ronde)
var TICKS_PER_QUARTER = 480;
var TICKS_PER_WHOLE = 1920;

/**
 * Calcul du plus grand commun diviseur (PGCD / GCD)
 */
function gcd(a, b) {
    a = Math.abs(Math.round(a));
    b = Math.abs(Math.round(b));
    while (b) {
        var t = b;
        b = a % b;
        a = t;
    }
    return a || 1;
}

/**
 * Conversion d'un nombre de ticks MuseScore en fraction simplifiée { num, denom }
 */
function ticksToFraction(ticks) {
    if (!ticks || ticks <= 0) {
        return { num: 1, denom: 16 };
    }
    var g = gcd(ticks, TICKS_PER_WHOLE);
    var num = Math.round(ticks / g);
    var denom = Math.round(TICKS_PER_WHOLE / g);
    return { num: num, denom: denom };
}

/**
 * Conversion d'une fraction { num, denom } en nombre entier de ticks MuseScore
 */
function fractionToTicks(num, denom) {
    if (!num || !denom || denom <= 0) {
        return TICKS_PER_QUARTER;
    }
    return Math.round((num / denom) * TICKS_PER_WHOLE);
}

/**
 * Normalisation et parsing robuste d'une durée depuis n'importe quel format d'événement
 * (objet {num, denom}, tableau [1, 4], string "1/4", float 0.25, ou entier de ticks)
 */
function parseDuration(event) {
    if (!event) {
        return { num: 1, denom: 4 };
    }

    // 1. Déjà explicite { num, denom } ou { numerator, denominator }
    if (typeof event.num === "number" && typeof event.denom === "number" && event.denom > 0) {
        return { num: event.num, denom: event.denom };
    }
    if (typeof event.numerator === "number" && typeof event.denominator === "number" && event.denominator > 0) {
        return { num: event.numerator, denom: event.denominator };
    }

    // 2. Propriété duration imbriquée
    var dur = event.duration;
    if (dur) {
        if (typeof dur === "object") {
            if (typeof dur.num === "number" && typeof dur.denom === "number") {
                return { num: dur.num, denom: dur.denom };
            }
            if (typeof dur.numerator === "number" && typeof dur.denominator === "number") {
                return { num: dur.numerator, denom: dur.denominator };
            }
            if (Array.isArray(dur) && dur.length >= 2) {
                return { num: parseInt(dur[0], 10), denom: parseInt(dur[1], 10) };
            }
        } else if (typeof dur === "string") {
            var parts = dur.split("/");
            if (parts.length === 2) {
                return { num: parseInt(parts[0], 10) || 1, denom: parseInt(parts[1], 10) || 4 };
            }
        } else if (typeof dur === "number") {
            // Durée exprimée en fractions de ronde (ex: 0.25 = noire, 0.5 = blanche, 0.125 = croche)
            var ticks = Math.round(dur * TICKS_PER_WHOLE);
            return ticksToFraction(ticks);
        }
    }

    // 3. Durée en ticks directe
    if (typeof event.ticks === "number" && event.ticks > 0) {
        return ticksToFraction(event.ticks);
    }

    // Valeur par défaut : Noire (1/4)
    return { num: 1, denom: 4 };
}

/**
 * Détermine si un événement représente un silence
 */
function isRestEvent(event) {
    if (!event) return false;
    if (event.isRest === true || event.is_rest === true) return true;
    if (event.type === "rest" || event.type === "silence") return true;
    if (event.pitch === null || event.pitch === undefined) {
        if (event.pitches === null || event.pitches === undefined) {
            if (event.isNote === false) return true;
        }
    }
    return false;
}

/**
 * Récupère le pitch de la sélection courante dans MuseScore, ou fallback
 */
function getSelectedPitch(score, fallbackPitch) {
    var def = (fallbackPitch !== undefined && fallbackPitch !== null) ? fallbackPitch : 60; // C4 par défaut
    if (!score) return def;

    try {
        if (score.selection && score.selection.elements && score.selection.elements.length > 0) {
            for (var i = 0; i < score.selection.elements.length; i++) {
                var el = score.selection.elements[i];
                if (!el) continue;
                // Si l'élément est une note
                if (typeof el.pitch === "number") {
                    return el.pitch;
                }
                // Si l'élément est un accord (Chord)
                if (el.notes && el.notes.length > 0 && typeof el.notes[0].pitch === "number") {
                    return el.notes[0].pitch;
                }
            }
        }
    } catch (err) {
        console.log("[ScoreCursorWriter] getSelectedPitch notice: " + err);
    }
    return def;
}

/**
 * Calcule la position en ticks entiers d'un curseur
 */
function getCursorTick(cursor) {
    if (!cursor) return 0;
    try {
        if (cursor.tick) {
            if (typeof cursor.tick.ticks === "number") return cursor.tick.ticks;
            if (typeof cursor.tick === "number") return cursor.tick;
        }
        if (cursor.fraction) {
            if (typeof cursor.fraction.ticks === "number") return cursor.fraction.ticks;
        }
    } catch (e) {
        // Fallback
    }
    return 0;
}

/**
 * Calcule le nombre de ticks restants dans la mesure courante du curseur
 */
function getRemainingTicksInMeasure(cursor) {
    if (!cursor || !cursor.measure) {
        return TICKS_PER_WHOLE; // Fallback sécurisé
    }

    try {
        var mStart = 0;
        if (cursor.measure.tick) {
            mStart = (typeof cursor.measure.tick.ticks === "number") ? cursor.measure.tick.ticks :
                     (typeof cursor.measure.tick === "number") ? cursor.measure.tick : 0;
        }

        var mLength = TICKS_PER_WHOLE; // 4/4 par défaut = 1920 ticks
        if (cursor.measure.ticks) {
            mLength = (typeof cursor.measure.ticks.ticks === "number") ? cursor.measure.ticks.ticks :
                      (typeof cursor.measure.ticks === "number") ? cursor.measure.ticks : TICKS_PER_WHOLE;
        } else if (cursor.measure.timesigActual) {
            var tsNum = cursor.measure.timesigActual.numerator || 4;
            var tsDen = cursor.measure.timesigActual.denominator || 4;
            mLength = Math.round((tsNum / tsDen) * TICKS_PER_WHOLE);
        }

        var curTick = getCursorTick(cursor);
        var remaining = (mStart + mLength) - curTick;
        return (remaining > 0) ? remaining : 0;
    } catch (err) {
        console.log("[ScoreCursorWriter] getRemainingTicksInMeasure notice: " + err);
        return TICKS_PER_WHOLE;
    }
}

/**
 * Applique une liaison (tie) sur la dernière note ajoutée au curseur
 */
function applyTieAtCursor(cursor, score) {
    if (!cursor || !score) return;
    try {
        cursor.prev();
        if (cursor.element) {
            if (cursor.element.type === 104 /* NOTE */ || cursor.element.pitch !== undefined) {
                score.selection.select(cursor.element, false);
                cmd("tie");
            } else if (cursor.element.notes && cursor.element.notes.length > 0) {
                score.selection.select(cursor.element.notes[0], false);
                cmd("tie");
            }
        }
        cursor.next();
    } catch (err) {
        console.log("[ScoreCursorWriter] applyTieAtCursor notice: " + err);
    }
}

/**
 * Prépare et positionne un nouveau curseur sur la partition
 */
function initCursor(score, staffIdx, voice) {
    var cursor = score.newCursor();
    // Tente de démarrer à la sélection courante (1 = SELECTION_START)
    cursor.rewind(1);
    if (!cursor.segment) {
        // Fallback au début de la partition (0 = SCORE_START)
        cursor.rewind(0);
    }
    
    var s = (staffIdx !== undefined && staffIdx !== null) ? parseInt(staffIdx, 10) : 0;
    var v = (voice !== undefined && voice !== null) ? parseInt(voice, 10) : 0;
    
    cursor.staffIdx = s;
    cursor.voice = v;
    cursor.track = (s * 4) + v;
    
    return cursor;
}

// ============================================================================
// FONCTION 1 : Injection Rythme Seul (Mode 1)
// ============================================================================

/**
 * writeRhythmEvents:
 * Injecte les durées de notes et silences quantifiés.
 * Si aucun pitch n'est spécifié, utilise la note actuellement sélectionnée ou C4 (pitch 60).
 * 
 * @param {Array} events - Liste d'événements rythmiques quantifiés
 * @param {number} pitchDefaut - Pitch MIDI optionnel (ex: 60)
 * @param {number} staffIdx - Index de portée (défaut 0)
 * @param {number} voice - Index de voix (défaut 0)
 * @param {Object} scoreObj - Référence score optionnelle (fallback curScore)
 * @returns {Object} Résultat de l'opération { success, count, mode }
 */
function writeRhythmEvents(events, pitchDefaut, staffIdx, voice, scoreObj) {
    var score = scoreObj || (typeof curScore !== "undefined" ? curScore : null);
    if (!score) {
        console.log("[ScoreCursorWriter] Erreur : Aucune partition active trouvée.");
        return { success: false, error: "No active score" };
    }

    if (!events || !Array.isArray(events) || events.length === 0) {
        console.log("[ScoreCursorWriter] Avertissement : Aucun événement rythmique à injecter.");
        return { success: true, count: 0 };
    }

    // Détermination du pitch cible
    var targetPitch = getSelectedPitch(score, pitchDefaut);
    var staff = (staffIdx !== undefined && staffIdx !== null) ? staffIdx : 0;
    var v = (voice !== undefined && voice !== null) ? voice : 0;

    var insertedCount = 0;
    score.startCmd();
    try {
        var cursor = initCursor(score, staff, v);

        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            if (!ev) continue;

            var dur = parseDuration(ev);
            cursor.setDuration(dur.num, dur.denom);

            if (isRestEvent(ev)) {
                cursor.addRest();
            } else {
                var p = (typeof ev.pitch === "number") ? ev.pitch : targetPitch;
                cursor.addNote(p, false);
                insertedCount++;
            }
        }
        console.log("[ScoreCursorWriter] Rythme injecté avec succès (" + insertedCount + " notes, " + events.length + " événements).");
        return { success: true, count: insertedCount, totalEvents: events.length, mode: "rhythm" };
    } catch (err) {
        console.log("[ScoreCursorWriter] Exception lors de writeRhythmEvents : " + err);
        return { success: false, error: String(err) };
    } finally {
        score.endCmd();
    }
}

// ============================================================================
// FONCTION 2 : Injection Piano Polyphonique (Mode 2)
// ============================================================================

/**
 * writePianoEvents:
 * Injecte les accords polyphoniques (addToChord = true), gère les deux portées
 * (main droite clé de sol staff 0, main gauche clé de fa staff 1).
 * 
 * @param {Array|Object} events - Événements polyphoniques (accords ou flux séparés)
 * @param {number} staffIdx - Portée forcée optionnelle (si null, split automatique Sol/Fa)
 * @param {Object} scoreObj - Référence score optionnelle
 * @returns {Object} Résultat de l'opération { success, count, mode }
 */
function writePianoEvents(events, staffIdx, scoreObj) {
    var score = scoreObj || (typeof curScore !== "undefined" ? curScore : null);
    if (!score) {
        console.log("[ScoreCursorWriter] Erreur : Aucune partition active trouvée.");
        return { success: false, error: "No active score" };
    }

    if (!events) {
        return { success: true, count: 0 };
    }

    var insertedNotes = 0;
    var insertedChords = 0;

    score.startCmd();
    try {
        // Cas A : Objet avec flux séparés { treble: [...], bass: [...] } ou { staff0: [...], staff1: [...] }
        if (!Array.isArray(events) && typeof events === "object") {
            var trebleEvents = events.treble || events.right || events.staff0 || [];
            var bassEvents = events.bass || events.left || events.staff1 || [];

            if (trebleEvents.length > 0) {
                var resTreble = injectPianoStaffEvents(score, trebleEvents, 0);
                insertedNotes += resTreble.notes;
                insertedChords += resTreble.chords;
            }
            if (bassEvents.length > 0) {
                var resBass = injectPianoStaffEvents(score, bassEvents, 1);
                insertedNotes += resBass.notes;
                insertedChords += resBass.chords;
            }
            return { success: true, count: insertedNotes, chords: insertedChords, mode: "piano" };
        }

        // Cas B : Tableau plat d'événements
        if (staffIdx !== undefined && staffIdx !== null) {
            // Portée explicite spécifiée
            var res = injectPianoStaffEvents(score, events, staffIdx);
            return { success: true, count: res.notes, chords: res.chords, mode: "piano" };
        }

        // Cas C : Split automatique main droite (Sol >= 60) / main gauche (Fa < 60)
        var rightHandEvents = [];
        var leftHandEvents = [];

        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            if (!ev) continue;

            if (ev.staff === 1 || ev.hand === "left" || ev.hand === "lh") {
                leftHandEvents.push(ev);
            } else if (ev.staff === 0 || ev.hand === "right" || ev.hand === "rh") {
                rightHandEvents.push(ev);
            } else {
                // Détection par hauteur de note (split point C4 = 60)
                var pitches = extractPitches(ev);
                if (pitches.length === 0) {
                    rightHandEvents.push(ev);
                } else {
                    var avgPitch = 0;
                    for (var p = 0; p < pitches.length; p++) avgPitch += pitches[p];
                    avgPitch /= pitches.length;

                    if (avgPitch >= 60) {
                        rightHandEvents.push(ev);
                    } else {
                        leftHandEvents.push(ev);
                    }
                }
            }
        }

        if (rightHandEvents.length > 0) {
            var rRes = injectPianoStaffEvents(score, rightHandEvents, 0);
            insertedNotes += rRes.notes;
            insertedChords += rRes.chords;
        }
        if (leftHandEvents.length > 0) {
            var lRes = injectPianoStaffEvents(score, leftHandEvents, 1);
            insertedNotes += lRes.notes;
            insertedChords += lRes.chords;
        }

        console.log("[ScoreCursorWriter] Piano polyphonique injecté (" + insertedNotes + " notes, " + insertedChords + " accords).");
        return { success: true, count: insertedNotes, chords: insertedChords, mode: "piano" };
    } catch (err) {
        console.log("[ScoreCursorWriter] Exception lors de writePianoEvents : " + err);
        return { success: false, error: String(err) };
    } finally {
        score.endCmd();
    }
}

/**
 * Extrait un tableau de hauteurs MIDI d'un événement
 */
function extractPitches(ev) {
    if (!ev) return [];
    if (Array.isArray(ev.pitches)) return ev.pitches;
    if (Array.isArray(ev.notes)) {
        var res = [];
        for (var i = 0; i < ev.notes.length; i++) {
            if (typeof ev.notes[i] === "number") res.push(ev.notes[i]);
            else if (ev.notes[i] && typeof ev.notes[i].pitch === "number") res.push(ev.notes[i].pitch);
        }
        return res;
    }
    if (typeof ev.pitch === "number") return [ev.pitch];
    return [];
}

/**
 * Injection d'accords et silences sur une portée spécifique via addToChord
 */
function injectPianoStaffEvents(score, staffEvents, staffIdx) {
    var cursor = initCursor(score, staffIdx, 0);
    var notesCount = 0;
    var chordsCount = 0;

    for (var i = 0; i < staffEvents.length; i++) {
        var ev = staffEvents[i];
        if (!ev) continue;

        var dur = parseDuration(ev);
        cursor.setDuration(dur.num, dur.denom);

        if (isRestEvent(ev)) {
            cursor.addRest();
            continue;
        }

        var chordPitches = extractPitches(ev);
        if (chordPitches.length === 0) {
            cursor.addRest();
            continue;
        }

        // Injection polyphonique : 1ère note avec addToChord = false (avance), suivantes avec addToChord = true
        for (var p = 0; p < chordPitches.length; p++) {
            var pitch = chordPitches[p];
            var addToChord = (p > 0);
            cursor.addNote(pitch, addToChord);
            notesCount++;
        }
        chordsCount++;
    }

    return { notes: notesCount, chords: chordsCount };
}

// ============================================================================
// FONCTION 3 : Injection Chant Mélodique (Mode 3)
// ============================================================================

/**
 * writeVocalEvents:
 * Injecte la ligne mélodique monophonique avec liaisons (cmd("tie"))
 * si une note chevauche une barre de mesure ou demande une tenue.
 * 
 * @param {Array} events - Liste d'événements mélodiques monophoniques quantifiés
 * @param {number} staffIdx - Index de portée (défaut 0)
 * @param {number} voice - Index de voix (défaut 0)
 * @param {Object} scoreObj - Référence score optionnelle
 * @returns {Object} Résultat de l'opération { success, count, mode }
 */
function writeVocalEvents(events, staffIdx, voice, scoreObj) {
    var score = scoreObj || (typeof curScore !== "undefined" ? curScore : null);
    if (!score) {
        console.log("[ScoreCursorWriter] Erreur : Aucune partition active trouvée.");
        return { success: false, error: "No active score" };
    }

    if (!events || !Array.isArray(events) || events.length === 0) {
        console.log("[ScoreCursorWriter] Avertissement : Aucun événement vocal à injecter.");
        return { success: true, count: 0 };
    }

    var staff = (staffIdx !== undefined && staffIdx !== null) ? staffIdx : 0;
    var v = (voice !== undefined && voice !== null) ? voice : 0;
    var insertedNotes = 0;
    var tiedNotes = 0;

    score.startCmd();
    try {
        var cursor = initCursor(score, staff, v);

        for (var i = 0; i < events.length; i++) {
            var ev = events[i];
            if (!ev) continue;

            var dur = parseDuration(ev);

            // Gestion du silence
            if (isRestEvent(ev)) {
                cursor.setDuration(dur.num, dur.denom);
                cursor.addRest();
                continue;
            }

            var pitch = (typeof ev.pitch === "number") ? ev.pitch : 60;
            var noteTicks = fractionToTicks(dur.num, dur.denom);
            var remainingTicks = getRemainingTicksInMeasure(cursor);

            // Détection du chevauchement de barre de mesure
            if (remainingTicks > 0 && noteTicks > remainingTicks) {
                // Note divisée par la barre de mesure
                var part1Ticks = remainingTicks;
                var part2Ticks = noteTicks - remainingTicks;

                var f1 = ticksToFraction(part1Ticks);
                var f2 = ticksToFraction(part2Ticks);

                // 1. Première partie dans la mesure courante
                cursor.setDuration(f1.num, f1.denom);
                cursor.addNote(pitch, false);
                insertedNotes++;

                // 2. Application de la liaison (tie) sur la note franchissant la barre
                applyTieAtCursor(cursor, score);
                tiedNotes++;

                // 3. Seconde partie dans la mesure suivante
                cursor.setDuration(f2.num, f2.denom);
                cursor.addNote(pitch, false);
                insertedNotes++;
            } else {
                // La note tient intégralement dans la mesure courante
                cursor.setDuration(dur.num, dur.denom);
                cursor.addNote(pitch, false);
                insertedNotes++;

                // Liaison explicite si demandée par l'algorithme (ex: tenue / legato vocal)
                if (ev.tied === true || ev.tiedToNext === true) {
                    applyTieAtCursor(cursor, score);
                    tiedNotes++;
                }
            }
        }

        console.log("[ScoreCursorWriter] Chant mélodique injecté (" + insertedNotes + " notes, " + tiedNotes + " liaisons).");
        return { success: true, count: insertedNotes, ties: tiedNotes, mode: "vocal" };
    } catch (err) {
        console.log("[ScoreCursorWriter] Exception lors de writeVocalEvents : " + err);
        return { success: false, error: String(err) };
    } finally {
        score.endCmd();
    }
}

// ============================================================================
// Auto-Test / Diagnostic d'injection
// ============================================================================

/**
 * testInjection: Permet de vérifier rapidement le fonctionnement des fonctions
 * sans dépendance au serveur WebSocket externe.
 */
function testInjection(mode, scoreObj) {
    var score = scoreObj || (typeof curScore !== "undefined" ? curScore : null);
    if (!score) return { success: false, error: "Score non disponible" };

    if (mode === "rhythm") {
        var rhythmSample = [
            { isRest: false, num: 1, denom: 4, pitch: 60 },
            { isRest: false, num: 1, denom: 8, pitch: 60 },
            { isRest: false, num: 1, denom: 8, pitch: 60 },
            { isRest: true,  num: 1, denom: 4 },
            { isRest: false, num: 1, denom: 4, pitch: 60 }
        ];
        return writeRhythmEvents(rhythmSample, 60, 0, 0, score);
    } else if (mode === "piano") {
        var pianoSample = [
            { pitches: [60, 64, 67], num: 1, denom: 2, staff: 0 }, // C Maj Main Droite
            { pitches: [48, 55],     num: 1, denom: 2, staff: 1 }, // C-G Main Gauche
            { pitches: [65, 69, 72], num: 1, denom: 2, staff: 0 }, // F Maj Main Droite
            { pitches: [41, 48],     num: 1, denom: 2, staff: 1 }  // F-C Main Gauche
        ];
        return writePianoEvents(pianoSample, null, score);
    } else if (mode === "vocal") {
        var vocalSample = [
            { pitch: 64, num: 1, denom: 4 },
            { pitch: 65, num: 1, denom: 4 },
            { pitch: 67, num: 1, denom: 2, tiedToNext: true },
            { pitch: 67, num: 1, denom: 4 },
            { isRest: true, num: 1, denom: 4 }
        ];
        return writeVocalEvents(vocalSample, 0, 0, score);
    }
    return { success: false, error: "Mode inconnu: " + mode };
}
