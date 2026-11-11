import Darwin
import Foundation

@main
struct BackendChecks {
  struct Failure: Error { let message: String }
  static func check(_ value: Bool, _ message: String) throws {
    if !value { throw Failure(message: message) }
  }
  static func wait(_ predicate: () -> Bool, seconds: Double = 5) throws {
    let deadline = Date().addingTimeInterval(seconds)
    while !predicate() && Date() < deadline {
      RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
    try check(predicate(), "bounded wait failed")
  }
  static func main() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(
      "backend check with spaces " + UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: root) }
    let interpreter = CommandLine.arguments.count > 2 ? URL(fileURLWithPath: CommandLine.arguments[2]) : root.appendingPathComponent("python3")
    if CommandLine.arguments.count <= 2 {
      try FileManager.default.createSymbolicLink(at: interpreter, withDestinationURL: URL(fileURLWithPath: "/usr/bin/python3"))
    }
    var fixtureDirectories: [URL] = []
    defer { for directory in fixtureDirectories { try? FileManager.default.removeItem(at: directory) } }
    let fixture = URL(fileURLWithPath: CommandLine.arguments[1]).path
    func configuration(mode: String, path: String = "health") throws -> (
      LaunchConfiguration, URL, URL
    ) {
      let reserve = LocalWebServer(root: root)
      let port = try reserve.start(port: 0)
      reserve.stop()
      let outputDirectory = FileManager.default.temporaryDirectory.appendingPathComponent("native-fixture-\(port)")
      try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
      fixtureDirectories.append(outputDirectory)
      let output = outputDirectory.appendingPathComponent("result.json")
      var values: [String: Any] = [
        "executable": interpreter.path, "arguments": [fixture, String(port), mode],
        "workingDirectory": root.path, "smokeWorkingDirectory": "{{SMOKE_DIR}}", "healthPath": path,
        "healthMarker": "fixture-ready", "stopOnQuit": true,
      ]
      if mode == "managed" {
        values["managedStateFile"] =
          output.deletingPathExtension().appendingPathExtension("state.json").path
        values["smokeManagedStateFile"] = values["managedStateFile"]
        values["managedScript"] = fixture
        values["smokeManagedScript"] = fixture
      }
      return (
        try JSONDecoder().decode(
          LaunchConfiguration.self, from: JSONSerialization.data(withJSONObject: values)),
        URL(string: "http://127.0.0.1:\(port)/")!, output
      )
    }
    let (config, url, output) = try configuration(mode: "normal")
    let runtime = try BackendRuntime(configuration: config, resources: root, smoke: true)
    defer { runtime.stop() }
    var ready: Bool?
    runtime.start(url: url, workspace: root.path) { ready = $0 }
    try wait({ ready != nil })
    try check(ready == true, "child did not become ready")
    let first = try JSONSerialization.jsonObject(with: Data(contentsOf: output)) as! [String: Any]
    let directory = first["cwd"] as! String
    try check(directory.contains("workspace-smoke-"), "smoke used a normal working directory")
    ready = nil
    runtime.start(url: url, workspace: root.path) { ready = $0 }
    try wait({ ready != nil })
    try check(ready == true, "retry did not reuse healthy child")
    let retry = try JSONSerialization.jsonObject(with: Data(contentsOf: output)) as! [String: Any]
    try check((first["pid"] as! Int) == (retry["pid"] as! Int), "retry spawned another child")
    runtime.stop()
    try wait({ !FileManager.default.fileExists(atPath: directory) })

    let (redirectConfig, redirectURL, redirectOutput) = try configuration(
      mode: "normal", path: "redirect")
    let redirect = try BackendRuntime(
      configuration: redirectConfig, resources: root, smoke: true, startupTimeout: 1)
    defer { redirect.stop() }
    var redirected: Bool?
    redirect.start(url: redirectURL, workspace: root.path) { redirected = $0 }
    try wait({ redirected != nil }, seconds: 12)
    try check(
      redirected == false
        && !FileManager.default.fileExists(
          atPath: redirectOutput.deletingPathExtension().appendingPathExtension("redirected").path),
      "readiness followed a redirect")

    let (managedConfig, managedURL, _) = try configuration(mode: "managed")
    let managed = try BackendRuntime(configuration: managedConfig, resources: root, smoke: true, startupTimeout: 2)
    defer { managed.stop() }
    var managedReady: Bool?
    managed.start(url: managedURL, workspace: root.path) { managedReady = $0 }
    try wait({ managedReady != nil })
    try check(managedReady == true, "managed state/script/listener verification failed")
    managed.stop()

    let foreign = Process()
    foreign.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    foreign.arguments = ["-c", "import time; time.sleep(60)"]
    foreign.standardOutput = FileHandle.nullDevice
    foreign.standardError = FileHandle.nullDevice
    try foreign.run()
    defer {
      if foreign.isRunning {
        foreign.terminate()
        foreign.waitUntilExit()
      }
    }
    let (hungConfig, hungURL, hungOutput) = try configuration(mode: "hang")
    let hung = try BackendRuntime(configuration: hungConfig, resources: root, smoke: true)
    defer { hung.stop() }
    ready = nil
    hung.start(url: hungURL, workspace: root.path) { ready = $0 }
    try wait({ ready != nil })
    try check(ready == true, "hung fixture did not start")
    let info =
      try JSONSerialization.jsonObject(with: Data(contentsOf: hungOutput)) as! [String: Any]
    hung.stop()
    try check(kill(Int32(info["pid"] as! Int), 0) != 0, "owned child survived bounded shutdown")
    try check(foreign.isRunning, "unrelated process was stopped")
    try check(
      !FileManager.default.fileExists(atPath: info["cwd"] as! String), "smoke data was left behind")
    print(
      "PASS: child readiness, isolated state, retry reuse, redirect refusal, bounded owned shutdown and foreign-process preservation"
    )
  }
}
