@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

echo =====================================================================
echo  AudioScoreTranscriber - Configuration de la Jonction NTFS Live
echo =====================================================================
echo.

set TARGET_REPO=C:\Users\Jamet\Documents\code\AudioScoreTranscriber
set MS4_PLUGINS_DIR=C:\Users\Jamet\Documents\MuseScore4\Plugins
set JUNCTION_LINK=%MS4_PLUGINS_DIR%\AudioScoreTranscriber

echo [1/3] Verification du repertoire source...
if not exist %TARGET_REPO% (
    echo [ERREUR] Le repertoire source n'existe pas : %TARGET_REPO%
    goto :error
)
echo [OK] Source detectee : %TARGET_REPO%
echo.

echo [2/3] Verification du dossier Plugins de MuseScore 4...
if not exist %MS4_PLUGINS_DIR% (
    echo [INFO] Le dossier Plugins n'existe pas encore. Creation en cours...
    mkdir %MS4_PLUGINS_DIR%
    if errorlevel 1 (
        echo [ERREUR] Impossible de creer le dossier : %MS4_PLUGINS_DIR%
        goto :error
    )
)
echo [OK] Repertoire Plugins pret : %MS4_PLUGINS_DIR%
echo.

echo [3/3] Creation / Verification de la jonction NTFS...
if exist %JUNCTION_LINK% (
    echo [INFO] La liaison existe deja. Nettoyage de l'ancienne jonction...
    rmdir %JUNCTION_LINK%
)

mklink /J %JUNCTION_LINK% %TARGET_REPO%
if errorlevel 1 (
    echo [ERREUR] Echec de la creation de la jonction NTFS.
    goto :error
)

echo.
echo =====================================================================
echo [SUCCES] Jonction NTFS activee avec succes !
echo MuseScore 4 verra directement vos modifications sans aucune copie.
echo Destination : %JUNCTION_LINK%
echo Source      : %TARGET_REPO%
echo =====================================================================
goto :end

:error
echo.
echo [ECHEC] La configuration a rencontre une erreur.
pause
exit /b 1

:end
exit /b 0
