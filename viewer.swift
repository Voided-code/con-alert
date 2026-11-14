// CON alert viewer: an invisible browser window that keeps the game running in the
// background so the monitor can take pictures of it without a Safari tab.
//
//   conviewer <state-dir> <url>
//
// It has no Dock icon and never takes focus. The window is invisible and far off-screen until it is
// told to show itself (to log in, or to move the map by hand); closing it (the red button, Esc or
// Cmd-W) hides it again.
// Commands arrive as files  <state-dir>/viewer-req-<id>.json  = {"cmd": ..., ...}
// and are answered with      <state-dir>/viewer-res-<id>.json.
//   snapshot {path, js?}  save a picture of the page (.tiff is fastest, else PNG);
//                     an optional script is run first and its result returned
//   show / hide       put the window on screen / back off-screen
//   load {url}        open a URL        reload      reload the page
//   click {x,y}       a real mouse click at a page point (for pop-ups)
//   js {code}         run JavaScript in the page and return the result
//   info              just report        quit        exit
// Points are in page coordinates (pixels of the snapshot divided by "scale").

import Cocoa
import WebKit

let args = CommandLine.arguments
guard args.count >= 3 else { print("usage: conviewer <state-dir> <url>"); exit(2) }
let stateDir = URL(fileURLWithPath: args[1])
let startURL = args[2]
let pageW: CGFloat = 1512, pageH: CGFloat = 872
let offScreen = NSPoint(x: -30000, y: -30000)

// Silence the game: nothing here should ever make a sound.
let muteScript = """
(function () {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (AC) {
    AC.prototype.resume = function () { return Promise.resolve(); };
    const Wrapped = function (...a) { const c = new AC(...a); try { c.suspend(); } catch (e) {} return c; };
    Wrapped.prototype = AC.prototype;
    window.AudioContext = Wrapped; window.webkitAudioContext = Wrapped;
  }
  const st = document.createElement('style');
  st.textContent = '.wzrk-overlay, #wzrk_wrapper, .wzrk-alert { display: none !important; }';
  (document.head || document.documentElement).appendChild(st);
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () { this.muted = true; this.volume = 0; return play.call(this); };
})();
"""

// Calls a private Bool setter if this version of the system has it.
func setPrivate(_ obj: NSObject, _ name: String, _ value: Bool) {
    let sel = Selector((name))
    guard obj.responds(to: sel) else { return }
    typealias Fn = @convention(c) (AnyObject, Selector, Bool) -> Void
    unsafeBitCast(obj.method(for: sel), to: Fn.self)(obj, sel, value)
}

final class FreeWindow: NSWindow {
    // allow the window to live completely off-screen
    override func constrainFrameRect(_ r: NSRect, to screen: NSScreen?) -> NSRect { r }
    override var canBecomeKey: Bool { !ignoresMouseEvents }
    override var canBecomeMain: Bool { !ignoresMouseEvents }
}

final class Viewer: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let win: FreeWindow
    let web: WKWebView
    var shown = false
    var crashes = 0
    var watcher: DispatchSourceFileSystemObject?

    override init() {
        let cfg = WKWebViewConfiguration()
        // its own saved cookies, so a login made in this window is remembered
        cfg.websiteDataStore = WKWebsiteDataStore(forIdentifier: UUID(uuidString: "5D2B6C1E-6A0B-4C55-9B5C-C0A1E57A11E7")!)
        cfg.mediaTypesRequiringUserActionForPlayback = .all
        // a window nobody can see is normally put to sleep (slow timers); the game must keep running
        setPrivate(cfg.preferences, "_setPageVisibilityBasedProcessSuppressionEnabled:", false)
        setPrivate(cfg.preferences, "_setHiddenPageDOMTimerThrottlingEnabled:", false)
        setPrivate(cfg.preferences, "_setHiddenPageDOMTimerThrottlingAutoIncreases:", false)
        cfg.userContentController.addUserScript(
            WKUserScript(source: muteScript, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: pageW, height: pageH), configuration: cfg)
        web.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        win = FreeWindow(contentRect: NSRect(x: 0, y: 0, width: pageW, height: pageH),
                         styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        win.title = "CON alert - background game window (close it to hide it again)"
        win.contentView = web
        win.delegate = self
        win.isReleasedWhenClosed = false
        web.navigationDelegate = self
        web.uiDelegate = self
        // keep the page drawing while the window is off-screen or covered
        setPrivate(web, "_setWindowOcclusionDetectionEnabled:", false)
        hide()
        // displays plugged in, unplugged or rearranged: put a hidden window back out of the way
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            if let me = self, !me.shown { me.hide() }
        }
        // while it is on screen, Esc or Cmd-W hides it again (there is no menu bar to do it from)
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] e in
            guard let me = self, me.shown else { return e }
            if e.keyCode == 53 || (e.modifierFlags.contains(.command) && e.charactersIgnoringModifiers == "w") { me.hide(); return nil }
            return e
        }
        web.load(URLRequest(url: URL(string: startURL)!))
        // react to a command file the moment it appears; the timer is only a safety net
        let fd = open(stateDir.path, O_EVTONLY)
        if fd >= 0 {
            let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
            src.setEventHandler { [weak self] in self?.poll() }
            src.resume()
            watcher = src
        }
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.poll() }
    }

    // Hidden means: far off-screen AND see-through AND click-through AND underneath the desktop picture.
    // Off-screen alone is not enough: when displays are plugged in or rearranged, macOS pulls stray
    // windows back onto a screen, and the "hidden" window would then sit there, impossible to close.
    func hide() {
        shown = false
        win.ignoresMouseEvents = true
        win.alphaValue = 0
        win.hasShadow = false
        win.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopWindow)) - 1)
        win.collectionBehavior = [.stationary, .ignoresCycle, .fullScreenNone, .canJoinAllSpaces]
        win.setFrameOrigin(offScreen)
        win.orderBack(nil)                    // stays "open" so the page keeps running
    }

    func show() {
        shown = true
        win.alphaValue = 1
        win.hasShadow = true
        win.level = .normal
        win.ignoresMouseEvents = false
        win.collectionBehavior = [.fullScreenNone]        // no full-screen mode: a full-screen window cannot be tucked away
        if let s = NSScreen.main?.visibleFrame {
            // top edge just under the menu bar, so the title bar and its close button are always reachable
            win.setFrameOrigin(NSPoint(x: max(s.minX, s.midX - win.frame.width / 2), y: s.maxY - win.frame.height))
        } else { win.center() }
        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { hide(); return false }

    // links that want a new window open in this one
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigationAction.targetFrame == nil { webView.load(navigationAction.request) }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        crashes += 1
        webView.reload()                      // the game ran out of memory or crashed: start it again
    }

    // ------------------------------------------------------------ commands
    func poll() {
        guard let files = try? FileManager.default.contentsOfDirectory(atPath: stateDir.path) else { return }
        for name in files where name.hasPrefix("viewer-req-") && name.hasSuffix(".json") {
            let url = stateDir.appendingPathComponent(name)
            guard let data = try? Data(contentsOf: url) else { continue }
            try? FileManager.default.removeItem(at: url)
            guard let req = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
            let id = String(name.dropFirst("viewer-req-".count).dropLast(".json".count))
            handle(req) { extra in self.respond(id, extra) }
        }
    }

    func respond(_ id: String, _ extra: [String: Any]) {
        var res: [String: Any] = [
            "ok": true, "url": web.url?.absoluteString ?? "", "title": web.title ?? "",
            "shown": shown, "loading": web.isLoading, "crashes": crashes,
            "scale": Double(win.backingScaleFactor), "w": Double(pageW), "h": Double(pageH)]
        for (k, v) in extra { res[k] = v }
        if let data = try? JSONSerialization.data(withJSONObject: res) {
            try? data.write(to: stateDir.appendingPathComponent("viewer-res-\(id).json"), options: .atomic)
        }
    }

    func handle(_ req: [String: Any], done: @escaping ([String: Any]) -> Void) {
        func num(_ k: String) -> CGFloat { CGFloat((req[k] as? NSNumber)?.doubleValue ?? 0) }
        switch req["cmd"] as? String ?? "" {
        case "snapshot":
            let path = req["path"] as? String ?? ""
            if let code = req["js"] as? String {
                web.evaluateJavaScript(code) { result, _ in
                    self.snapshot(to: path) { ok in done(["ok": ok, "result": result.map { "\($0)" } ?? ""]) }
                }
            } else {
                snapshot(to: path) { ok in done(["ok": ok]) }
            }
        case "show": show(); done([:])
        case "hide": hide(); done([:])
        case "reload": web.reload(); done([:])
        case "load":
            if let u = URL(string: req["url"] as? String ?? "") { web.load(URLRequest(url: u)); done([:]) }
            else { done(["ok": false]) }
        case "click":
            let p = NSPoint(x: num("x"), y: num("y"))
            mouse(.mouseMoved, p); mouse(.leftMouseDown, p); mouse(.leftMouseUp, p); done([:])
        case "js":
            web.evaluateJavaScript(req["code"] as? String ?? "") { result, error in
                if let error = error { done(["ok": false, "error": "\(error)"]) }
                else { done(["result": result.map { "\($0)" } ?? ""]) }
            }
        case "quit": done([:]); DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { exit(0) }
        case "toggle": if shown { hide() } else { show() }; done([:])
        case "signout":                       // forget the saved login (cookies and site data) of this window
            let store = web.configuration.websiteDataStore
            store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast) {
                self.web.load(URLRequest(url: URL(string: "about:blank")!)); done([:])
            }
        default: done([:])
        }
    }

    func snapshot(to path: String, done: @escaping (Bool) -> Void) {
        web.takeSnapshot(with: WKSnapshotConfiguration()) { img, _ in
            guard let img = img, let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
                done(false); return
            }
            let rep = NSBitmapImageRep(cgImage: cg)
            let tiff = path.hasSuffix(".tiff")        // uncompressed: several times faster than PNG
            guard let data = tiff
                    ? rep.representation(using: .tiff, properties: [.compressionMethod: NSNumber(value: NSBitmapImageRep.TIFFCompression.none.rawValue)])
                    : rep.representation(using: .png, properties: [:]) else {
                done(false); return
            }
            do { try data.write(to: URL(fileURLWithPath: path), options: .atomic); done(true) } catch { done(false) }
        }
    }

    // ------------------------------------------------------ pretend mouse click
    // page point (origin top-left) -> window point (origin bottom-left)
    func winPoint(_ p: NSPoint) -> NSPoint { web.convert(NSPoint(x: p.x, y: web.isFlipped ? p.y : pageH - p.y), to: nil) }

    func mouse(_ type: NSEvent.EventType, _ p: NSPoint) {
        guard let e = NSEvent.mouseEvent(with: type, location: winPoint(p), modifierFlags: [],
                                         timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: win.windowNumber,
                                         context: nil, eventNumber: 0, clickCount: 1,
                                         pressure: type == .leftMouseUp ? 0 : 1) else { return }
        switch type {
        case .leftMouseDown: web.mouseDown(with: e)
        case .leftMouseDragged: web.mouseDragged(with: e)
        case .leftMouseUp: web.mouseUp(with: e)
        default: web.mouseMoved(with: e)
        }
    }
}

// no App Nap for this process either
let awake = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .latencyCritical],
                                                  reason: "Keeps the background game updating")
let app = NSApplication.shared
app.setActivationPolicy(.accessory)           // no Dock icon, no menu bar
let viewer = Viewer()
signal(SIGTERM) { _ in exit(0) }
app.run()
