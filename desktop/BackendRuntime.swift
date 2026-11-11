import Darwin
import Foundation

struct LaunchConfiguration: Decodable {
  let executable: String
  let arguments: [String]
  let workspaceDirectory: String?
  let workingDirectory: String
  let environment: [String: String]?
  let healthPath: String
  let healthMarker: String
  let stopOnQuit: Bool
  let smokeArguments: [String]?
  let smokeEnvironment: [String: String]?
  let smokeWorkingDirectory: String?
  let managedStateFile: String?
  let managedScript: String?
  let smokeManagedStateFile: String?
  let smokeManagedScript: String?
}

/// One bounded read-only readiness probe. Response bodies never enter logs.
private final class ReadinessProbe: NSObject, URLSessionDataDelegate {
  private var bytes = Data()
  private var accepted = false
  private var session: URLSession?
  private let marker: String
  private var completion: ((Bool) -> Void)?
  init(url: URL, marker: String, completion: @escaping (Bool) -> Void) {
    self.marker = marker
    self.completion = completion
    super.init()
    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = 1
    config.timeoutIntervalForResource = 2
    config.httpMaximumConnectionsPerHost = 1
    session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    session!.dataTask(with: url).resume()
  }
  func urlSession(
    _ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
  ) {
    accepted =
      (response as? HTTPURLResponse)?.statusCode == 200 && response.expectedContentLength <= 131072
    completionHandler(accepted ? .allow : .cancel)
  }
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
    completionHandler: @escaping (URLRequest?) -> Void
  ) { completionHandler(nil) }
  func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
    if bytes.count + data.count > 131072 {
      accepted = false
      dataTask.cancel()
      return
    }
    bytes.append(data)
  }
  func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
    let success =
      accepted && error == nil && (String(data: bytes, encoding: .utf8)?.contains(marker) ?? false)
    let done = completion
    completion = nil
    bytes.removeAll()
    session.finishTasksAndInvalidate()
    self.session = nil
    DispatchQueue.main.async { done?(success) }
  }
}

/// Starts only its configured child. Existing service ownership stays with project launchers.
final class BackendRuntime {
  private var child: Process?
  private(set) var startupStage = "not-started"
  var smokeDirectory: String? { scratch?.path }
  private var activeProbe: ReadinessProbe?
  private var finished = false
  private var generation = 0
  private let configuration: LaunchConfiguration
  private let resources: URL
  private let smoke: Bool
  private let scratch: URL?
  private let startupTimeout: TimeInterval
  init(
    configuration: LaunchConfiguration, resources: URL, smoke: Bool,
    startupTimeout: TimeInterval = 35
  ) throws {
    self.configuration = configuration
    self.resources = resources
    self.smoke = smoke
    self.startupTimeout = min(35, max(0.5, startupTimeout))
    if smoke {
      let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first!.appendingPathComponent("NativeWorkspaceChecks")
      try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
      let path = base.appendingPathComponent("workspace-smoke-" + UUID().uuidString)
      try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
      scratch = path
    } else {
      scratch = nil
    }
  }
  func start(url: URL, workspace: String, completion: @escaping (Bool) -> Void) {
    generation += 1
    let currentGeneration = generation
    finished = false
    func resolve(_ value: String) -> String {
      value.replacingOccurrences(of: "{{RESOURCES}}", with: resources.path)
        .replacingOccurrences(of: "{{SMOKE_DIR}}", with: scratch?.path ?? "")
        .replacingOccurrences(of: "{{WORKSPACE}}", with: smoke ? scratch!.path : workspace)
        .replacingOccurrences(of: "{{HOME}}", with: FileManager.default.homeDirectoryForCurrentUser.path)
    }
    if !smoke && configuration.workspaceDirectory != nil {
      do { try FileManager.default.createDirectory(atPath: resolve(workspace), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]) }
      catch { completion(false); return }
    }
    let process = child?.isRunning == true ? child! : Process()
    if child?.isRunning != true {
      process.executableURL = URL(fileURLWithPath: resolve(configuration.executable))
      process.arguments =
        (smoke ? configuration.smokeArguments ?? configuration.arguments : configuration.arguments)
        .map(resolve)
      process.currentDirectoryURL = URL(
        fileURLWithPath: resolve(
          smoke
            ? configuration.smokeWorkingDirectory ?? configuration.workingDirectory
            : configuration.workingDirectory))
      let allowed = Set(["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "PATH"])
      var environment = ProcessInfo.processInfo.environment.filter { allowed.contains($0.key) }
      for (key, value) in configuration.environment ?? [:] { environment[key] = resolve(value) }
      if smoke {
        for (key, value) in configuration.smokeEnvironment ?? [:] {
          environment[key] = resolve(value)
        }
      }
      process.environment = environment
      process.standardInput = FileHandle.nullDevice
      process.standardOutput = FileHandle.nullDevice
      process.standardError = FileHandle.nullDevice
      process.terminationHandler = { [scratch = self.scratch] _ in
        if let scratch = scratch { try? FileManager.default.removeItem(at: scratch) }
      }
      do {
        try process.run()
        child = process
      } catch {
        completion(false)
        return
      }
    }
    let deadline = ProcessInfo.processInfo.systemUptime + startupTimeout
    func probe() {
      guard !self.finished, self.generation == currentGeneration else { return }
      if ProcessInfo.processInfo.systemUptime > deadline
        || (!process.isRunning && process.terminationStatus != 0)
      {
        self.startupStage = process.isRunning ? "startup-timeout" : "process-exit-" + String(process.terminationStatus)
        self.stop()
        completion(false)
        return
      }
      let statePath =
        self.smoke ? self.configuration.smokeManagedStateFile : self.configuration.managedStateFile
      let scriptPath =
        self.smoke
        ? self.configuration.smokeManagedScript ?? self.configuration.managedScript
        : self.configuration.managedScript
      let state = statePath.map { URL(fileURLWithPath: resolve($0)) }
      let script = scriptPath.map(resolve)
      DispatchQueue.global(qos: .userInitiated).async {
        let owned = ServiceOwnership.verified(
          port: UInt16(url.port!), child: process, state: state, script: script,
          workingDirectory: resolve(self.configuration.workingDirectory))
        DispatchQueue.main.async {
          guard !self.finished, self.generation == currentGeneration else { return }
          guard owned else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: probe)
            return
          }
          self.activeProbe = ReadinessProbe(
            url: url.appendingPathComponent(self.configuration.healthPath),
            marker: self.configuration.healthMarker
          ) { ready in
            self.activeProbe = nil
            guard !self.finished, self.generation == currentGeneration else { return }
            if ready {
              self.finished = true
              self.startupStage = "ready"
              completion(true)
            } else {
              DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: probe)
            }
          }
        }
      }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: probe)
  }
  func stop() {
    finished = true
    generation += 1
    if smoke || configuration.stopOnQuit, let process = child, process.isRunning {
      process.terminate()
      let deadline = ProcessInfo.processInfo.systemUptime + 8
      while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
        Thread.sleep(forTimeInterval: 0.025)
      }
      if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) }
      let reapDeadline = ProcessInfo.processInfo.systemUptime + 2
      while process.isRunning && ProcessInfo.processInfo.systemUptime < reapDeadline {
        Thread.sleep(forTimeInterval: 0.025)
      }
    }
    // Only this still-running Process can be killed; no foreign PID/port or process group is targeted.
    if let scratch = scratch, child?.isRunning != true {
      try? FileManager.default.removeItem(at: scratch)
    }
  }
}
