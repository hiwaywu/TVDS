package com.tvapp.player;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.net.Uri;
import android.os.Build;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.widget.Toast;

import java.lang.ref.WeakReference;

/**
 * JS bridge exposed to the WebView as `Android`.
 *
 * Must be a PUBLIC top-level class: private inner classes are silently skipped by
 * addJavascriptInterface on newer WebView builds, which looks exactly like "the
 * button does nothing".
 */
public class Bridge {

    private final WeakReference<MainActivity> act;

    public Bridge(MainActivity a) {
        this.act = new WeakReference<>(a);
    }

    private Activity a() {
        return act.get();
    }

    @JavascriptInterface
    public void openExternal(final String url) {
        final MainActivity m = act.get();
        if (m == null) return;
        m.runOnUiThread(new Runnable() {
            public void run() {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW);
                    i.setDataAndType(Uri.parse(url), "application/vnd.apple.mpegurl");
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    m.startActivity(Intent.createChooser(i, "Open stream with"));
                } catch (Exception e) {
                    Toast.makeText(m, "no player app for this stream", Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    @JavascriptInterface
    public void pickPlaylist() {
        final MainActivity m = act.get();
        if (m == null) return;
        m.runOnUiThread(new Runnable() {
            public void run() { m.openPlaylistPicker(); }
        });
    }

    @JavascriptInterface
    public void setOrientation(final String mode) {
        final MainActivity m = act.get();
        if (m == null) return;
        m.runOnUiThread(new Runnable() {
            public void run() {
                try {
                    Log.i("tvapp", "setOrientation(" + mode + ")");
                    if ("landscape".equals(mode)) {
                        m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
                    } else if ("portrait".equals(mode)) {
                        m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
                    } else {
                        m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
                    }
                } catch (Exception ignored) {
                }
            }
        });
    }

    @JavascriptInterface
    public void copy(final String text, final String label) {
        final MainActivity m = act.get();
        if (m == null) return;
        m.runOnUiThread(new Runnable() {
            public void run() {
                try {
                    ClipboardManager cm = (ClipboardManager) m.getSystemService(Context.CLIPBOARD_SERVICE);
                    cm.setPrimaryClip(ClipData.newPlainText(label == null ? "tvds" : label, text));
                    Toast.makeText(m, "copied " + (text.length() / 1024) + " KB to clipboard",
                            Toast.LENGTH_SHORT).show();
                } catch (Exception e) {
                    Toast.makeText(m, "clipboard failed", Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    @JavascriptInterface
    public String device() {
        return Build.MANUFACTURER + " " + Build.MODEL;
    }

    @JavascriptInterface
    public String ping() {
        return "ok";
    }
}
