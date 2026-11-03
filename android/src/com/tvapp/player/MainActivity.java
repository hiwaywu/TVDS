package com.tvapp.player;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

/**
 * The whole UI is assets/index.html driven by hls.js - the same player that runs
 * on the desktop. A WebView (rather than MediaPlayer) is the only thing that can
 * handle CMAF/fMP4 HLS like ABC 7's feed, which MediaPlayer rejects with -1007.
 *
 * Tuned for large foldables (Galaxy Z Fold): immersive, no zoom, survives the
 * fold/unfold config change without restarting playback.
 */
public class MainActivity extends Activity {

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        web = new WebView(this);
        setContentView(web, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        goImmersive();      // after setContentView: the DecorView does not exist before it

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);   // lets file:// pages XHR any CDN
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setTextZoom(100);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        if (Build.VERSION.SDK_INT >= 21) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);  // many relays are http
        }

        WebView.setWebContentsDebuggingEnabled(true);

        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.setBackgroundColor(0xFF0B0D10);
        web.addJavascriptInterface(new Bridge(), "Android");

        String auto = getIntent() == null ? null : getIntent().getStringExtra("play");
        if (auto != null && auto.length() > 0) {
            web.loadUrl("file:///android_asset/index.html?play=" + Uri.encode(auto));
        } else {
            web.loadUrl("file:///android_asset/index.html");
        }
    }

    private void goImmersive() {
        try {
            if (Build.VERSION.SDK_INT >= 30) {
                getWindow().setDecorFitsSystemWindows(false);
                WindowInsetsController c = getWindow().getInsetsController();
                if (c != null) {
                    c.hide(WindowInsets.Type.systemBars());
                    c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                }
            } else {
                getWindow().getDecorView().setSystemUiVisibility(
                        View.SYSTEM_UI_FLAG_FULLSCREEN
                                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
            }
        } catch (Throwable ignored) {
            // immersive is cosmetic - never let it take the app down
        }
    }

    /** Hand a stream to an external player - the escape hatch for HEVC / AC-3 feeds. */
    private class Bridge {
        @JavascriptInterface
        public void openExternal(final String url) {
            runOnUiThread(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                    i.setDataAndType(Uri.parse(url), "application/vnd.apple.mpegurl");
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(Intent.createChooser(i, "Open stream with"));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "no player app for this stream",
                            Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public String device() {
            return Build.MANUFACTURER + " " + Build.MODEL;
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            web.evaluateJavascript("window.tvappBack ? window.tvappBack() : false", value -> {
                if (!"true".equals(value)) finish();
            });
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) goImmersive();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.loadUrl("about:blank");
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
