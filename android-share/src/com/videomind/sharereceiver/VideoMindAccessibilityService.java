package com.videomind.sharereceiver;

import android.accessibilityservice.AccessibilityService;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 无障碍服务：实现「一步收藏」。
 *
 * 背景（实测）：
 *  - 抖音分享面板对无障碍几乎隐形（面板节点 text 为空、按钮点击不上报），
 *    所以「监听点击 + 匹配按钮文字」这条路不通。
 *  - 但无障碍服务本身**可以读剪贴板 API**（实验证明：调用返回空串而非异常）。
 *
 * 方案：**定时轮询剪贴板**。用户点「分享链接」把短链写入剪贴板后，
 * 服务在 1.5 秒内读到并自动上传，用户无需再打开任何 App。
 */
public class VideoMindAccessibilityService extends AccessibilityService {

    private static final String TAG = "VideoMindA11y";
    private static final Pattern DOUYIN = Pattern.compile(
            "https?://v\\.douyin\\.com/[A-Za-z0-9._~%\\-/?#=&]+");
    private static final long POLL_MS = 1500;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private String lastSeen = "";     // 上次处理过的剪贴板内容，避免重复上传
    private boolean polling = false;

    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            if (!polling) return;
            try {
                checkClipboard();
            } catch (Exception e) {
                Log.e(TAG, "轮询出错", e);
            }
            handler.postDelayed(this, POLL_MS);
        }
    };

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        Log.i(TAG, "无障碍服务已连接，启动剪贴板轮询");
        polling = true;
        handler.post(poller);
    }

    @Override
    public boolean onUnbind(Intent intent) {
        polling = false;
        handler.removeCallbacks(poller);
        Log.i(TAG, "服务解绑，停止轮询");
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        polling = false;
        handler.removeCallbacks(poller);
        super.onDestroy();
    }

    private void checkClipboard() {
        String text = readClipboard();
        if (text == null || text.isEmpty()) return;
        if (text.equals(lastSeen)) return;          // 内容没变，跳过

        Matcher m = DOUYIN.matcher(text);
        if (!m.find()) {
            // 剪贴板变了但不是抖音链接（比如你在复制别的东西），记下但不处理
            lastSeen = text;
            return;
        }
        String url = m.group(0);
        lastSeen = text;
        Log.i(TAG, "剪贴板发现抖音链接，自动收藏: " + url);

        Intent svc = new Intent(this, UploadService.class);
        svc.putExtra("url", url);
        svc.putExtra("raw", text);
        startService(svc);
    }

    private String readClipboard() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip() || cm.getPrimaryClip() == null) return "";
            if (cm.getPrimaryClip().getItemCount() == 0) return "";
            CharSequence t = cm.getPrimaryClip().getItemAt(0).getText();
            return t == null ? "" : t.toString();
        } catch (Exception e) {
            Log.e(TAG, "读剪贴板失败", e);
            return "";
        }
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 不需要事件驱动；保留空实现（config 仍订阅事件以便系统保持服务活跃）。
    }

    @Override
    public void onInterrupt() {
        Log.i(TAG, "无障碍服务被中断");
    }
}
