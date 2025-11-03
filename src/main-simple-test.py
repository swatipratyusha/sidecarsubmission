#!/usr/bin/env python3
"""
Simple Chrome test script for website compatibility testing
Just opens Chrome and keeps it open for manual testing via VNC
"""

import os
import time
from playwright.sync_api import sync_playwright

def main():
    """Open Chrome and keep it open for testing"""
    print("🚀 Starting simple Chrome test...")
    
    with sync_playwright() as p:
        # Launch Chrome with persistent context to enable CDP access
        # Using persistent context allows HTTP-based CDP connections
        context = p.chromium.launch_persistent_context(
            user_data_dir="/tmp/chrome_user_data",
            headless=False,  # Show browser window
            channel="chrome",  # Use real Google Chrome
            locale="en-US",
            timezone_id="America/New_York",
            accept_downloads=True,
            permissions=["geolocation", "notifications"],
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--exclude-switches=enable-automation",
                "--disable-automation",
                "--remote-debugging-port=9222",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--allow-running-insecure-content",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--disable-remote-fonts",  # Don't wait for web fonts to load
            ]
        )
        
        page = context.pages[0] if context.pages else context.new_page()
        
        page.add_init_script("""
            // Hide webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
            
            // Fake plugins (realistic browser plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                    {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}
                ],
            });
            
            // Set realistic languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            
            // Add chrome object
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            
            // Mock permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            
            // Add realistic connection
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 100,
                    downlink: 10,
                    saveData: false
                })
            });
            
            // Mock battery API
            Object.defineProperty(navigator, 'getBattery', {
                get: () => () => Promise.resolve({
                    charging: true,
                    chargingTime: 0,
                    dischargingTime: Infinity,
                    level: 1
                })
            });
            
            // Hide automation-related properties
            delete navigator.__proto__.webdriver;
            
            // Mock media devices
            Object.defineProperty(navigator, 'mediaDevices', {
                get: () => ({
                    enumerateDevices: () => Promise.resolve([
                        {deviceId: 'default', kind: 'audioinput', label: '', groupId: ''},
                        {deviceId: 'default', kind: 'audiooutput', label: '', groupId: ''},
                        {deviceId: 'default', kind: 'videoinput', label: '', groupId: ''}
                    ])
                })
            });
            
            // Set realistic hardware concurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: () => 8
            });
            
            // Set device memory
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8
            });
            
            // Mock notification permission
            Object.defineProperty(Notification, 'permission', {
                get: () => 'default'
            });
        """)
        
        # Set realistic HTTP headers for all requests
        context.set_extra_http_headers({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
        
        # Navigate to a simple starting page
        print("🌐 Opening Chrome with Google homepage...")
        page.goto("https://www.google.com", timeout=10000)
        
        print("✅ Chrome is now open!")
        print("🔗 Browser accessible via CDP on port 9222")
        print("🌐 Connect via VNC: http://localhost:6080/vnc.html")
        print("📝 Test your websites manually in the browser")
        print("⏳ Browser will stay open for 10 minutes...")
        print("💡 Press Ctrl+C to stop early")
        
        try:
            # Keep browser open for 10 minutes
            for i in range(600):  # 600 seconds = 10 minutes
                time.sleep(1)
                if i % 60 == 0:  # Print every minute
                    minutes_left = (600 - i) // 60
                    print(f"⏰ {minutes_left} minutes remaining...")
                    
        except KeyboardInterrupt:
            print("\n🛑 Stopping early...")
        
        print("🔄 Closing browser...")
        context.close()
        print("✅ Test completed!")

if __name__ == "__main__":
    main() 