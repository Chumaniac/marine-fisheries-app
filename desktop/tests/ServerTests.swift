import Foundation

@main
struct ServerChecks {
  static func main() throws {
    let manager = FileManager.default
    let parent = manager.temporaryDirectory.appendingPathComponent(
      "workspace-http-" + UUID().uuidString
    ).resolvingSymlinksInPath()
    let root = parent.appendingPathComponent("ui")
    try manager.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? manager.removeItem(at: parent) }
    try Data("synthetic interface".utf8).write(to: root.appendingPathComponent("index.html"))
    try Data("private fixture".utf8).write(to: parent.appendingPathComponent("outside.txt"))
    try manager.createSymbolicLink(
      at: root.appendingPathComponent("linked.txt"),
      withDestinationURL: parent.appendingPathComponent("outside.txt"))
    let server = LocalWebServer(root: root)
    let port = try server.start(port: 0)
    let host = "127.0.0.1:\(port)"
    defer { server.stop() }
    for (method, path, requestHost, status) in [
      ("GET", "/", host, 200), ("HEAD", "/", host, 200),
      ("POST", "/", host, 405), ("GET", "/", "outside.example", 403),
      ("GET", "/../outside.txt", host, 400), ("GET", "/%2e%2e/outside.txt", host, 400),
      ("GET", "/.env", host, 400), ("GET", "/linked.txt", host, 404),
      ("GET", "/missing.js", host, 404), ("GET", "/folder/", host, 404),
    ] {
      precondition(
        server.response(method: method, target: path, host: requestHost).0 == status,
        "HTTP contract failed")
    }
    let second = LocalWebServer(root: root)
    do {
      _ = try second.start(port: port)
      fatalError("An occupied port was claimed")
    } catch {}
    let complete = DispatchSemaphore(value: 0)
    var received = false
    URLSession.shared.dataTask(with: URL(string: "http://127.0.0.1:\(port)/")!) {
      data, response, error in
      received =
        error == nil && (response as? HTTPURLResponse)?.statusCode == 200
        && data == Data("synthetic interface".utf8)
      complete.signal()
    }.resume()
    precondition(
      complete.wait(timeout: .now() + 5) == .success && received, "Loopback transfer failed")
    server.stop()
    let replacement = try second.start(port: port)
    precondition(replacement == port, "Owned listener could not reopen")
    second.stop()
    print(
      "PASS: bounded loopback assets, traversal/symlink/host/method refusals, occupied port and reopen"
    )
  }
}
