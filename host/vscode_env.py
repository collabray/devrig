#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DevRig-HID :: host/vscode_env.py

Runs a dedicated, isolated VS Code instance (its own --user-data-dir, all
extensions disabled) whose settings remove every editor behaviour that would
change typed text: auto-closing brackets/quotes, auto-indent, suggestions,
formatters, snippet/emmet expansion, whitespace trimming, CRLF conversion.
"""
import json
import os
import subprocess

# Settings that make the editor a *literal* text sink and make disk state a
# reliable mirror of the editor buffer (auto-save after 500 ms).
SETTINGS = {
    # --- literal input ----------------------------------------------------
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoClosingOvertype": "never",
    "editor.autoClosingDelete": "never",
    "editor.autoSurround": "never",
    "editor.autoIndent": "none",
    "editor.autoIndentOnPaste": False,
    "editor.formatOnType": False,
    "editor.formatOnPaste": False,
    "editor.formatOnSave": False,
    "editor.codeActionsOnSave": {},
    "editor.linkedEditing": False,
    "editor.detectIndentation": False,
    "editor.insertSpaces": False,
    "editor.tabSize": 4,
    "editor.trimAutoWhitespace": False,
    "editor.useTabStops": False,
    "editor.tabCompletion": "off",
    "editor.acceptSuggestionOnEnter": "off",
    "editor.acceptSuggestionOnCommitCharacter": False,
    "editor.quickSuggestions": {"other": False, "comments": False, "strings": False},
    "editor.quickSuggestionsDelay": 10000,
    "editor.suggestOnTriggerCharacters": False,
    "editor.snippetSuggestions": "none",
    "editor.wordBasedSuggestions": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.hover.enabled": False,
    "editor.occurrencesHighlight": "off",
    "editor.lightbulb.enabled": "off",
    "editor.dropIntoEditor.enabled": False,
    "editor.unicodeHighlight.ambiguousCharacters": False,
    "editor.wordWrap": "off",
    "emmet.triggerExpansionOnTab": False,
    "emmet.showExpandedAbbreviation": "never",
    "html.autoClosingTags": False,
    "html.autoCreateLinkedEditing": False,
    "javascript.autoClosingTags": False,
    "typescript.autoClosingTags": False,
    "javascript.suggest.autoImports": False,
    "typescript.suggest.autoImports": False,
    "javascript.updateImportsOnFileMove.enabled": "never",
    "typescript.updateImportsOnFileMove.enabled": "never",
    "json.format.enable": False,
    "github.copilot.enable": {"*": False},
    # --- files: exact bytes on disk -------------------------------------------
    "files.eol": "\n",
    "files.encoding": "utf8",
    "files.autoGuessEncoding": False,
    "files.trimTrailingWhitespace": False,
    "files.trimFinalNewlines": False,
    "files.insertFinalNewline": False,
    "files.autoSave": "afterDelay",
    "files.autoSaveDelay": 500,
    "files.hotExit": "off",
    "files.watcherExclude": {"**/node_modules/**": True, "**/.git/**": True},
    # --- quiet IDE ---------------------------------------------------------------
    "workbench.startupEditor": "none",
    "workbench.editor.enablePreview": False,
    "workbench.editor.restoreViewState": False,
    "workbench.tips.enabled": False,
    "workbench.welcomePage.walkthroughs.openOnInstall": False,
    "workbench.enableExperiments": False,
    "window.restoreWindows": "none",
    "window.confirmBeforeClose": "never",
    "window.dialogStyle": "custom",
    "security.workspace.trust.enabled": False,
    "telemetry.telemetryLevel": "off",
    "update.mode": "none",
    "update.showReleaseNotes": False,
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    "extensions.ignoreRecommendations": True,
    "git.enabled": False,
    "git.autofetch": False,
    "problems.decorations.enabled": False,
}


def _flags():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


class VSCode(object):
    def __init__(self, code_path, user_data_dir, workspace, logger=None):
        self.code = code_path
        self.user_data_dir = str(user_data_dir)
        self.workspace = str(workspace)
        self.log = logger

    def write_settings(self):
        user_dir = os.path.join(self.user_data_dir, "User")
        os.makedirs(user_dir, exist_ok=True)
        path = os.path.join(user_dir, "settings.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(SETTINGS, fh, indent=2)
        return path

    def _base(self):
        return [self.code, "--user-data-dir", self.user_data_dir, "--disable-extensions",
                "--disable-workspace-trust"]

    def _run(self, extra, timeout=90):
        argv = self._base() + extra
        if self.log:
            self.log.debug("vscode: %s", " ".join(argv))
        cp = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, **_flags())
        if cp.returncode != 0:
            raise RuntimeError("VS Code CLI failed (rc=%d): %s"
                               % (cp.returncode, cp.stderr.decode("utf-8", "replace").strip()))

    def launch(self):
        self.write_settings()
        self._run(["--new-window", self.workspace])

    def open_file(self, path, line=1, col=1):
        self._run(["--reuse-window", "--goto", "%s:%d:%d" % (path, line, col)])
