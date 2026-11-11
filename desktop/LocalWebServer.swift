import Darwin
import Foundation

/// Static assets only: no filesystem APIs, uploads, directory listings or SPA rewrites.
final class LocalWebServer {
  let root: URL
  private var socket: Int32 = -1
  private let lifecycle = NSLock()
  private func listener() -> Int32 {
    lifecycle.lock()
    defer { lifecycle.unlock() }
    return socket
  }
  private let capacity = DispatchSemaphore(value: 12)
  private let queue = DispatchQueue(
    label: "local.assets", qos: .userInitiated, attributes: .concurrent)
  private(set) var port: UInt16 = 0
  init(root: URL) { self.root = root.resolvingSymlinksInPath().standardizedFileURL }

  func response(method: String, target: String, host: String) -> (Int, String, Data) {
    guard method == "GET" || method == "HEAD" else { return (405, "text/plain", Data()) }
    guard host == "127.0.0.1:\(port)" else { return (403, "text/plain", Data()) }
    let raw = String(
      target.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)[0])
    guard let path = raw.removingPercentEncoding, path.hasPrefix("/"),
      !path.contains("\\"), !path.contains("\0"),
      !path.split(separator: "/").contains(where: { $0.hasPrefix(".") })
    else {
      return (400, "text/plain", Data())
    }
    let file = root.appendingPathComponent(path == "/" ? "index.html" : String(path.dropFirst()))
      .standardizedFileURL
    guard file.path.hasPrefix(root.path + "/"), file.resolvingSymlinksInPath().path == file.path,
      let values = try? file.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey]),
      values.isRegularFile == true, (values.fileSize ?? Int.max) <= 8 * 1024 * 1024
    else { return (404, "text/plain", Data()) }
    let fd = Darwin.open(file.path, O_RDONLY | O_NOFOLLOW)
    guard fd >= 0 else { return (404, "text/plain", Data()) }
    let stream = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    defer { try? stream.close() }
    guard let bytes = try? stream.read(upToCount: 8 * 1024 * 1024 + 1),
      bytes.count <= 8 * 1024 * 1024
    else { return (404, "text/plain", Data()) }
    let mime =
      [
        "html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
        "css": "text/css; charset=utf-8", "json": "application/json", "svg": "image/svg+xml",
        "png": "image/png", "jpg": "image/jpeg", "woff2": "font/woff2",
        "txt": "text/plain; charset=utf-8",
      ][file.pathExtension] ?? "application/octet-stream"
    return (200, mime, bytes)
  }

  func start(port requested: UInt16) throws -> UInt16 {
    let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { throw NSError(domain: "LocalAssets", code: 1) }
    var reuse: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
    var address = sockaddr_in()
    address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_family = sa_family_t(AF_INET)
    address.sin_port = requested.bigEndian
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let bound = withUnsafePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
      }
    }
    guard bound == 0, listen(fd, 12) == 0 else {
      Darwin.close(fd)
      throw NSError(domain: "LocalAssets", code: 2)
    }
    var length = socklen_t(MemoryLayout<sockaddr_in>.size)
    withUnsafeMutablePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = getsockname(fd, $0, &length) }
    }
    port = UInt16(bigEndian: address.sin_port)
    lifecycle.lock()
    socket = fd
    lifecycle.unlock()
    queue.async { [weak self] in
      while let self = self, self.listener() == fd {
        let connection = accept(fd, nil, nil)
        if connection < 0 { break }
        guard self.capacity.wait(timeout: .now()) == .success else {
          Darwin.close(connection)
          continue
        }
        self.queue.async {
          self.serve(connection)
          self.capacity.signal()
        }
      }
    }
    return port
  }

  private func serve(_ fd: Int32) {
    defer { Darwin.close(fd) }
    var timeout = timeval(tv_sec: 4, tv_usec: 0)
    var noSignal: Int32 = 1
    setsockopt(
      fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
    setsockopt(
      fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
    setsockopt(
      fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal)))
    let started = ProcessInfo.processInfo.systemUptime
    var request = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while request.count < 16384 {
      guard ProcessInfo.processInfo.systemUptime - started < 4 else { return }
      let count = recv(fd, &buffer, min(buffer.count, 16384 - request.count), 0)
      guard count > 0 else { return }
      request.append(contentsOf: buffer.prefix(count))
      if request.range(of: Data("\r\n\r\n".utf8)) != nil { break }
    }
    guard let text = String(data: request, encoding: .utf8), text.contains("\r\n\r\n") else {
      return
    }
    let lines = text.components(separatedBy: "\r\n")
    let line = lines[0].split(separator: " ")
    guard line.count == 3 else { return }
    let host =
      lines.dropFirst().first(where: { $0.lowercased().hasPrefix("host:") })?.dropFirst(5)
      .trimmingCharacters(in: .whitespaces) ?? ""
    let result = response(method: String(line[0]), target: String(line[1]), host: host)
    let headers =
      "HTTP/1.1 \(result.0) \(result.0 == 200 ? "OK" : "Unavailable")\r\nContent-Type: \(result.1)\r\nContent-Length: \(result.2.count)\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nCross-Origin-Resource-Policy: same-origin\r\nContent-Security-Policy: default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'\r\n\r\n"
    var output = Data(headers.utf8)
    if line[0] != "HEAD" { output.append(result.2) }
    output.withUnsafeBytes { bytes in
      var offset = 0
      while offset < bytes.count {
        if ProcessInfo.processInfo.systemUptime - started > 12 { break }
        let sent = send(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset, 0)
        if sent <= 0 { break }
        offset += sent
      }
    }
  }
  func stop() {
    lifecycle.lock()
    let fd = socket
    socket = -1
    lifecycle.unlock()
    if fd >= 0 {
      shutdown(fd, SHUT_RDWR)
      Darwin.close(fd)
    }
  }
  deinit { stop() }
}
