package com.videomind.sharereceiver;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.ClipboardManager;
import android.content.Intent;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import android.widget.Button;
import android.widget.Toast;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** User-triggered capture only. No background clipboard monitoring. */
public class VideoMindAccessibilityService extends AccessibilityService {
    private static final String DY = "com.ss.android.ugc.aweme";
    private static final String TAG = "VideoMindA11y";
    private static final Pattern URL = Pattern.compile("https?://v\\.douyin\\.com/[A-Za-z0-9._~%\\-/?#=&]+");
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WindowManager wm;
    private WindowManager.LayoutParams params;
    private Button button;
    private boolean busy;
    private long started;
    private int attempts;

    @Override protected void onServiceConnected() {
        if (button != null && wm != null) wm.removeView(button);
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        button = new Button(this);
        button.setText("存入知识库");
        button.setTextSize(13);
        params = new WindowManager.LayoutParams(-2, -2,
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.LEFT;
        params.x = 24;
        params.y = 320;
        button.setOnClickListener(v -> begin());
        wm.addView(button, params);
        updateVisibility();
        Log.i(TAG, "User-triggered capture ready");
    }

    private AccessibilityNodeInfo douyinRoot() {
        for (AccessibilityWindowInfo window : getWindows()) {
            if (window.getType() != AccessibilityWindowInfo.TYPE_APPLICATION) continue;
            AccessibilityNodeInfo root = window.getRoot();
            if (root != null && DY.contentEquals(root.getPackageName() == null ? "" : root.getPackageName())) return root;
        }
        return null;
    }
    private void updateVisibility() {
        if (button != null && !busy) button.setVisibility(douyinRoot() == null ? View.GONE : View.VISIBLE);
    }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) { updateVisibility(); }
    @Override public void onInterrupt() { finish("操作已中断"); }

    private void begin() {
        if (busy) return;
        AccessibilityNodeInfo root = douyinRoot();
        if (root == null) return;
        busy = true;
        started = System.currentTimeMillis();
        button.setText("正在取链接…");
        attempts = 0;
        AccessibilityNodeInfo link = find(root, true);
        if (link != null) { copy(link); return; }
        AccessibilityNodeInfo share = find(root, false);
        if (share == null || !click(share)) { finish("未找到分享按钮，请回到视频页面"); return; }
        handler.postDelayed(this::waitForLink, 350);
    }
    private AccessibilityNodeInfo find(AccessibilityNodeInfo n, boolean link) {
        if (n == null) return null;
        String text = String.valueOf(n.getText());
        String desc = String.valueOf(n.getContentDescription());
        boolean match = link ? ("分享链接".equals(text) || "复制链接".equals(text))
                : desc.matches("^分享[0-9万亿.,，\\s]*.*按钮.*$");
        if (match && n.isVisibleToUser()) return n;
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo result = find(n.getChild(i), link);
            if (result != null) return result;
        }
        return null;
    }
    private boolean click(AccessibilityNodeInfo n) {
        AccessibilityNodeInfo current = n;
        for (int i = 0; current != null && i < 4; i++, current = current.getParent()) {
            if (current.isClickable() && current.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true;
        }
        Rect bounds = new Rect();
        n.getBoundsInScreen(bounds);
        if (bounds.isEmpty()) return false;
        Path path = new Path();
        path.moveTo(bounds.centerX(), bounds.centerY());
        return dispatchGesture(new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, 80)).build(), null, null);
    }
    private void waitForLink() {
        if (!busy) return;
        AccessibilityNodeInfo link = find(douyinRoot(), true);
        if (link != null) { copy(link); return; }
        if (++attempts >= 12) { finish("未找到分享链接，请先打开分享面板"); return; }
        handler.postDelayed(this::waitForLink, 300);
    }
    private void copy(AccessibilityNodeInfo link) {
        if (!click(link)) { finish("无法点击分享链接"); return; }
        Log.i(TAG, "Copy link action dispatched");
        handler.postDelayed(() -> {
            if (!busy) return;
            params.flags &= ~WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE;
            wm.updateViewLayout(button, params);
            button.requestFocus();
            attempts = 0;
            handler.postDelayed(this::readFocusedClipboard, 200);
        }, 700);
    }
    private void readFocusedClipboard() {
        if (!busy) return;
        ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        if (button.hasWindowFocus() && cm != null && cm.hasPrimaryClip()
                && cm.getPrimaryClipDescription() != null
                && cm.getPrimaryClipDescription().getTimestamp() >= started) {
            android.content.ClipData clip = cm.getPrimaryClip();
            if (clip != null && clip.getItemCount() > 0) {
                CharSequence raw = clip.getItemAt(0).getText();
                Matcher match = URL.matcher(raw == null ? "" : raw.toString());
                if (match.find()) {
                    Intent upload = new Intent(this, UploadService.class);
                    upload.putExtra("url", match.group());
                    upload.putExtra("raw", raw.toString());
                    startService(upload);
                    Log.i(TAG, "Fresh link captured with window focus");
                    finish(null);
                    handler.postDelayed(() -> {
                        AccessibilityNodeInfo root = douyinRoot();
                        if (root != null && containsCopyConfirmation(root)) performGlobalAction(GLOBAL_ACTION_BACK);
                    }, 350);
                    return;
                }
            }
        }
        if (++attempts >= 12) { finish("未取得新链接，请重试（没有上传旧剪贴板）"); return; }
        handler.postDelayed(this::readFocusedClipboard, 200);
    }
    private void finish(String message) {
        handler.removeCallbacksAndMessages(null);
        busy = false;
        if (button != null) {
            params.flags |= WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE;
            wm.updateViewLayout(button, params);
            button.setText("存入知识库");
            handler.postDelayed(this::updateVisibility, 250);
        }
        if (message != null) {
            Log.i(TAG, message);
            Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        }
    }
    private boolean containsCopyConfirmation(AccessibilityNodeInfo node) {
        if (String.valueOf(node.getText()).contains("链接已复制成功")) return true;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && containsCopyConfirmation(child)) return true;
        }
        return false;
    }
    @Override public boolean onUnbind(Intent intent) {
        handler.removeCallbacksAndMessages(null);
        busy = false;
        if (button != null) { wm.removeView(button); button = null; }
        return super.onUnbind(intent);
    }
    @Override public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (button != null) wm.removeView(button);
        super.onDestroy();
    }
}
