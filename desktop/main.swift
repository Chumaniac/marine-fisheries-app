import AppKit
import WebKit

struct AppConfiguration: Decodable {
  let name: String
  let identifier: String
  let mark: String
  let port: UInt16
  let sourceCommit: String
  let includesWorkingTreeChanges: Bool
  let mode: String
  let path: String?
  let launch: LaunchConfiguration?
  let closeGuardSelector: String?
  let smokePort: UInt16?
  let saveSelector: String?
  let readySelector: String?
  let workspaceSelectable: Bool?
}

final class DesktopApplication: NSObject, NSApplicationDelegate, NSWindowDelegate,
  WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate
{
  let configuration: AppConfiguration
  let resources: URL
  var helpWindow: NSWindow?
  var window: NSWindow!
  var web: WKWebView!
  var server: LocalWebServer?
  var backend: BackendRuntime?
  var origin: URL!
  var closingApproved = false
  var smokeCompleted = false
  var smokeSucceeded = false
  var loading: NSStackView!
  var loadingLabel: NSTextField!
  var retryButton: NSButton!
  var terminationSignal: DispatchSourceSignal?
  var lastSmokePage: [String: Any] = [:]
  let smokeOutput: URL?
  let snapshotOutput: URL?

  init(configuration: AppConfiguration, resources: URL) {
    self.configuration = configuration
    self.resources = resources
    func value(_ flag: String) -> URL? {
      guard let index = CommandLine.arguments.firstIndex(of: flag),
        index + 1 < CommandLine.arguments.count
      else { return nil }
      return URL(fileURLWithPath: CommandLine.arguments[index + 1])
    }
    smokeOutput = value("--smoke-check")
    snapshotOutput = value("--snapshot")
  }
  func applicationDidFinishLaunching(_ notification: Notification) {
    NSApp.appearance = NSAppearance(named: .darkAqua)
    signal(SIGTERM, SIG_IGN)
    terminationSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    terminationSignal?.setEventHandler { NSApp.terminate(nil) }
    terminationSignal?.resume()
    installMenus()
    let settings = WKWebViewConfiguration()
    // Presentation/storage context only; never an authentication or permission signal.
    settings.userContentController.addUserScript(
      WKUserScript(
        source: "Object.defineProperty(window,'__NATIVE_WORKSPACE__',{value:true});",
        injectionTime: .atDocumentStart, forMainFrameOnly: true))
    if smokeOutput != nil {
      settings.userContentController.addUserScript(
        WKUserScript(
          source:
            "window.__nativeLaunchErrors=0;addEventListener('error',()=>window.__nativeLaunchErrors++,true);addEventListener('unhandledrejection',()=>window.__nativeLaunchErrors++);",
          injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }
    if smokeOutput != nil { settings.websiteDataStore = .nonPersistent() }
    web = WKWebView(frame: .zero, configuration: settings)
    web.navigationDelegate = self
    web.uiDelegate = self
    var width = 1180
    if smokeOutput != nil, let index = CommandLine.arguments.firstIndex(of: "--width"),
      index + 1 < CommandLine.arguments.count, let value = Int(CommandLine.arguments[index + 1]),
      (320...1800).contains(value)
    {
      width = value
    }
    window = NSWindow(
      contentRect: NSRect(x: 0, y: 0, width: width, height: 790),
      styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false
    )
    window.title = configuration.name
    window.titlebarAppearsTransparent = true
    window.minSize = NSSize(width: smokeOutput == nil ? 760 : min(760, width), height: 540)
    window.delegate = self
    let container = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 790))
    web.frame = container.bounds
    web.autoresizingMask = [.width, .height]
    web.isHidden = true
    container.addSubview(web)
    loadingLabel = NSTextField(labelWithString: "正在打开 " + configuration.name)
    loadingLabel.textColor = .secondaryLabelColor
    loadingLabel.font = .systemFont(ofSize: 14)
    retryButton = NSButton(title: "重试", target: self, action: #selector(beginLoading))
    retryButton.isHidden = true
    loading = NSStackView(views: [loadingLabel, retryButton])
    loading.orientation = .vertical
    loading.spacing = 14
    loading.translatesAutoresizingMaskIntoConstraints = false
    container.addSubview(loading)
    NSLayoutConstraint.activate([
      loading.centerXAnchor.constraint(equalTo: container.centerXAnchor),
      loading.centerYAnchor.constraint(equalTo: container.centerYAnchor),
    ])
    window.contentView = container
    window.center()
    if smokeOutput == nil {
      window.makeKeyAndOrderFront(nil)
      NSApp.activate(ignoringOtherApps: true)
    } else {
      window.orderBack(nil)
    }
    beginLoading()
    if smokeOutput != nil {
      DispatchQueue.main.asyncAfter(deadline: .now() + 48) {
        if !self.smokeCompleted { self.finishSmoke(success: false, page: self.lastSmokePage) }
      }
    }
  }
  @objc func beginLoading() {
    web.isHidden = true
    loading.isHidden = false
    loadingLabel.stringValue = "正在打开 " + configuration.name
    retryButton.isHidden = true
    do {
      if configuration.mode == "static" {
        if server == nil {
          let assets = LocalWebServer(root: resources.appendingPathComponent("ui"))
          let port = try assets.start(port: smokeOutput == nil ? configuration.port : 0)
          server = assets
          origin = URL(string: "http://127.0.0.1:\(port)/")!
        }
        web.load(URLRequest(url: origin))
      } else if let launch = configuration.launch {
        let port =
          smokeOutput == nil ? configuration.port : configuration.smokePort ?? configuration.port
        origin = URL(string: "http://127.0.0.1:\(port)/")!
        if backend == nil {
          backend = try BackendRuntime(
            configuration: launch, resources: resources, smoke: smokeOutput != nil)
        }
        backend!.start(
          url: origin,
          workspace: UserDefaults.standard.string(forKey: "selectedWorkspace")
            ?? launch.workspaceDirectory ?? launch.workingDirectory
        ) { ready in
          if ready {
            self.web.load(
              URLRequest(url: self.origin.appendingPathComponent(self.configuration.path ?? "")))
          } else {
            self.showFailure()
          }
        }
      } else {
        showFailure()
      }
    } catch { showFailure() }
  }
  func installMenus() {
    let menu = NSMenu()
    let app = NSMenu()
    let edit = NSMenu()
    let appItem = NSMenuItem()
    appItem.submenu = app
    menu.addItem(appItem)
    app.addItem(
      withTitle: "关于 \(configuration.name)",
      action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    app.addItem(.separator())
    app.addItem(
      withTitle: "退出 \(configuration.name)", action: #selector(NSApplication.terminate(_:)),
      keyEquivalent: "q")
    if configuration.saveSelector != nil || configuration.workspaceSelectable == true {
      let fileItem = NSMenuItem(title: "文件", action: nil, keyEquivalent: "")
      let file = NSMenu()
      fileItem.submenu = file
      menu.addItem(fileItem)
      if configuration.workspaceSelectable == true {
        let open = NSMenuItem(
          title: "打开工作目录…", action: #selector(chooseWorkspace), keyEquivalent: "o")
        open.target = self
        file.addItem(open)
      }
      if configuration.saveSelector != nil {
        let save = NSMenuItem(title: "保存记录", action: #selector(saveContent), keyEquivalent: "s")
        save.target = self
        file.addItem(save)
      }
    }
    let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
    editItem.submenu = edit
    menu.addItem(editItem)
    for (title, action, key) in [
      ("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"),
      ("全选", "selectAll:", "a"),
    ] { edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key) }
    let viewItem = NSMenuItem(title: "显示", action: nil, keyEquivalent: "")
    let view = NSMenu()
    viewItem.submenu = view
    menu.addItem(viewItem)
    let reload = NSMenuItem(title: "重新载入", action: #selector(reloadPage), keyEquivalent: "r")
    reload.target = self
    view.addItem(reload)
    let helpItem = NSMenuItem(title: "帮助", action: nil, keyEquivalent: "")
    let help = NSMenu()
    helpItem.submenu = help
    menu.addItem(helpItem)
    let guide = NSMenuItem(title: "使用指南", action: #selector(showGuide), keyEquivalent: "?")
    guide.target = self
    help.addItem(guide)
    NSApp.helpMenu = help
    NSApp.mainMenu = menu
  }
  @objc func showGuide() {
    if let helpWindow = helpWindow { helpWindow.makeKeyAndOrderFront(nil); return }
    let guide = resources.appendingPathComponent("UserGuide.html")
    guard FileManager.default.fileExists(atPath: guide.path) else { return }
    let settings = WKWebViewConfiguration()
    settings.websiteDataStore = .nonPersistent()
    let page = WKWebView(frame: .zero, configuration: settings)
    page.loadFileURL(guide, allowingReadAccessTo: resources)
    let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 850, height: 720),
      styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
    panel.title = configuration.name + " 使用指南"
    panel.isReleasedWhenClosed = false
    panel.contentView = page
    panel.center()
    panel.makeKeyAndOrderFront(nil)
    helpWindow = panel
  }
  @objc func chooseWorkspace() {
    checkUnsaved { allowed in
      guard allowed else { return }
      let panel = NSOpenPanel()
      panel.canChooseFiles = false
      panel.canChooseDirectories = true
      panel.allowsMultipleSelection = false
      panel.beginSheetModal(for: self.window) { result in
        guard result == .OK, let url = panel.url else { return }
        self.backend?.stop()
        self.backend = nil
        UserDefaults.standard.set(url.path, forKey: "selectedWorkspace")
        self.beginLoading()
      }
    }
  }
  @objc func saveContent() {
    guard let selector = configuration.saveSelector else { return }
    let encoded = String(
      data: try! JSONSerialization.data(withJSONObject: [selector]), encoding: .utf8)!
    web.evaluateJavaScript("document.querySelector(\(encoded)[0])?.click()")
  }
  @objc func reloadPage() { checkUnsaved { if $0 { self.web.reload() } } }
  func checkUnsaved(_ completion: @escaping (Bool) -> Void) {
    guard web != nil, !web.isLoading else {
      completion(true)
      return
    }
    let selector = configuration.closeGuardSelector ?? ""
    let encoded = String(
      data: try! JSONSerialization.data(withJSONObject: [selector]), encoding: .utf8)!
    web.evaluateJavaScript(
      "(()=>{const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);const selector=\(encoded)[0];return event.defaultPrevented||!!(selector&&document.querySelector(selector));})()"
    ) { value, error in
      guard (value as? Bool) == true || error != nil else {
        completion(true)
        return
      }
      let alert = NSAlert()
      alert.messageText = "还有未保存或未完成的工作"
      alert.informativeText = "关闭或重新载入可能丢失编辑或中断当前操作。已保存的内容仍会保留。"
      alert.addButton(withTitle: "继续编辑")
      alert.addButton(withTitle: "继续关闭")
      alert.beginSheetModal(for: self.window) { completion($0 == .alertSecondButtonReturn) }
    }
  }
  func windowShouldClose(_ sender: NSWindow) -> Bool {
    if closingApproved { return true }
    checkUnsaved {
      if $0 {
        self.closingApproved = true
        self.window.close()
      }
    }
    return false
  }
  func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
    if closingApproved { return .terminateNow }
    checkUnsaved {
      if $0 { self.closingApproved = true }
      sender.reply(toApplicationShouldTerminate: $0)
    }
    return .terminateLater
  }
  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
  func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool
  {
    window.makeKeyAndOrderFront(nil)
    return true
  }
  func applicationWillTerminate(_ notification: Notification) {
    server?.stop()
    backend?.stop()
    if smokeOutput != nil { exit(smokeSucceeded ? 0 : 1) }
  }
  func showFailure() {
    if smokeOutput != nil {
      finishSmoke(success: false, page: [:])
      return
    }
    web.isHidden = true
    loading.isHidden = false
    loadingLabel.stringValue = "工作区暂时无法打开。请检查本机运行环境，再重试。"
    retryButton.isHidden = false
  }
  func webView(
    _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
    decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
  ) {
    if navigationAction.shouldPerformDownload {
      decisionHandler(.download)
      return
    }
    guard let url = navigationAction.request.url else {
      decisionHandler(.cancel)
      return
    }
    if (url.scheme == "http" && url.host == "127.0.0.1" && url.port == origin?.port)
      || url.scheme == "blob" || url.absoluteString == "about:blank"
      || (url.absoluteString == "about:srcdoc"
        && navigationAction.targetFrame?.isMainFrame == false)
    {
      decisionHandler(.allow)
      return
    }
    if navigationAction.navigationType == .linkActivated
      && ["https", "http"].contains(url.scheme ?? "")
    {
      NSWorkspace.shared.open(url)
    }
    decisionHandler(.cancel)
  }
  func webView(
    _ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload
  ) { download.delegate = self }
  func webView(
    _ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload
  ) { download.delegate = self }
  func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
    web.isHidden = false
    loading.isHidden = true
    guard smokeOutput != nil, !smokeCompleted else { return }
    checkSmokePage()
  }
  func checkSmokePage() {
    guard !smokeCompleted else { return }
    let selector = configuration.readySelector ?? "main"
    let encoded = String(
      data: try! JSONSerialization.data(withJSONObject: [selector]), encoding: .utf8)!
    web.evaluateJavaScript(
      "({title:document.title,ready:document.readyState,main:!!document.querySelector(\(encoded)[0]),errors:window.__nativeLaunchErrors||0,width:innerWidth,scrollWidth:document.documentElement.scrollWidth})"
    ) { value, error in
      guard !self.smokeCompleted else { return }
      let page = value as? [String: Any] ?? [:]
      self.lastSmokePage = page
      if error != nil || (page["errors"] as? Int ?? 0) > 0 {
        self.finishSmoke(success: false, page: page)
        return
      }
      if page["main"] as? Bool == true && page["ready"] as? String == "complete" {
        self.finishSmoke(success: true, page: page)
      } else {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.checkSmokePage() }
      }
    }
  }
  func finishSmoke(success: Bool, page: [String: Any]) {
    guard !smokeCompleted else { return }
    smokeCompleted = true
    smokeSucceeded = success
    backend?.stop()
    let report: [String: Any] = [
      "app": configuration.name, "success": success, "windowVisible": window?.isVisible ?? false,
      "sourceCommit": configuration.sourceCommit,
      "includesWorkingTreeChanges": configuration.includesWorkingTreeChanges,
      "isolatedWebsiteData": true, "runtimeStage": backend?.startupStage ?? "static", "smokeDirectory": backend?.smokeDirectory ?? "", "page": page,
    ]
    func finish() {
      if let path = self.smokeOutput,
        let bytes = try? JSONSerialization.data(
          withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
      {
        try? bytes.write(to: path)
      }
      self.closingApproved = true
      NSApp.terminate(nil)
    }
    if let path = snapshotOutput, success {
      web.takeSnapshot(with: nil) { image, _ in
        if let data = image?.tiffRepresentation,
          let png = NSBitmapImageRep(data: data)?.representation(using: .png, properties: [:])
        {
          try? png.write(to: path)
        }
        finish()
      }
    } else {
      finish()
    }
  }
  func webView(
    _ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
    initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void
  ) {
    let alert = NSAlert()
    alert.messageText = String(message.prefix(1500))
    alert.addButton(withTitle: "确定")
    alert.beginSheetModal(for: window) { _ in completionHandler() }
  }
  func webView(
    _ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
    initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void
  ) {
    let alert = NSAlert()
    alert.messageText = String(message.prefix(1500))
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "确定")
    alert.beginSheetModal(for: window) { completionHandler($0 == .alertSecondButtonReturn) }
  }
  func webView(
    _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
    withError error: Error
  ) { showFailure() }
  func webView(
    _ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
    defaultText: String?, initiatedByFrame frame: WKFrameInfo,
    completionHandler: @escaping (String?) -> Void
  ) {
    let alert = NSAlert()
    alert.messageText = String(prompt.prefix(1500))
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "确定")
    let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
    input.stringValue = defaultText ?? ""
    alert.accessoryView = input
    alert.beginSheetModal(for: window) {
      completionHandler($0 == .alertSecondButtonReturn ? input.stringValue : nil)
    }
  }
  func webView(
    _ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
    initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void
  ) {
    let panel = NSOpenPanel()
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection
    panel.canChooseDirectories = parameters.allowsDirectories
    panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
  }
  func download(
    _ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
    completionHandler: @escaping (URL?) -> Void
  ) {
    let panel = NSSavePanel()
    panel.nameFieldStringValue = (suggestedFilename as NSString).lastPathComponent
    panel.directoryURL =
      FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
    panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.url : nil) }
  }
}

func renderIcon(mark: String, directory: URL) throws {
  try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
  for (name, pixels) in [
    ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256),
    ("icon_256x256@2x", 512), ("icon_512x512", 512), ("icon_512x512@2x", 1024),
  ] {
    let side = CGFloat(pixels)
    let bitmap = NSBitmapImageRep(
      bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
      samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
      bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSColor(srgbRed: 0.105882, green: 0.290196, blue: 0.376471, alpha: 1).setFill()
    NSBezierPath(
      roundedRect: NSRect(
        x: side * 0.035, y: side * 0.035, width: side * 0.93, height: side * 0.93),
      xRadius: side * 0.22, yRadius: side * 0.22
    ).fill()
    let font = NSFont.systemFont(ofSize: side * (mark.count > 1 ? 0.33 : 0.5), weight: .medium)
    let text = NSAttributedString(
      string: mark,
      attributes: [.font: font, .foregroundColor: NSColor.white])
    let size = text.size()
    text.draw(at: NSPoint(x: (side - size.width) / 2, y: (side - size.height) / 2 + side * 0.01))
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(
      to: directory.appendingPathComponent(name + ".png"))
  }
}

let resources = Bundle.main.resourceURL!
do {
  let data = try Data(contentsOf: resources.appendingPathComponent("app.json"))
  let config = try JSONDecoder().decode(AppConfiguration.self, from: data)
  if let index = CommandLine.arguments.firstIndex(of: "--render-icon"),
    index + 1 < CommandLine.arguments.count
  {
    try renderIcon(
      mark: config.mark, directory: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
    exit(0)
  }
  let application = NSApplication.shared
  application.setActivationPolicy(.regular)
  let delegate = DesktopApplication(configuration: config, resources: resources)
  application.delegate = delegate
  application.run()
} catch {
  fputs("APPLICATION_CONFIGURATION_UNAVAILABLE\n", stderr)
  exit(1)
}
