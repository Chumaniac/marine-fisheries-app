import Darwin
import Foundation

/// Verify the listener before exposing its origin to persistent application storage.
enum ServiceOwnership {
  static func output(_ executable: String, _ arguments: [String]) -> String? {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_TIME": "C", "LC_CTYPE": "en_US.UTF-8"]
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do { try process.run() } catch { return nil }
    let deadline = ProcessInfo.processInfo.systemUptime + 2
    while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline {
      Thread.sleep(forTimeInterval: 0.01)
    }
    if process.isRunning {
      kill(process.processIdentifier, SIGKILL)
      return nil
    }
    guard process.terminationStatus == 0,
      let data = try? pipe.fileHandleForReading.read(upToCount: 16385), data.count <= 16384
    else { return nil }
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
  }
  static func managedPID(state: URL, script: String, executable: String, workingDirectory: String, port: UInt16)
    -> Int32?
  {
    guard state.resolvingSymlinksInPath().path == state.standardizedFileURL.path,
      let info = try? FileManager.default.attributesOfItem(atPath: state.path),
      info[.type] as? FileAttributeType == .typeRegular,
      (info[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
      let permissions = info[.posixPermissions] as? Int, permissions & 0o022 == 0,
      let size = info[.size] as? Int, size <= 4096,
      let data = try? Data(contentsOf: state),
      let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
      let pid = value["pid"] as? Int, pid > 1, pid <= Int(Int32.max),
      value["port"] as? Int == Int(port), let stamp = value["started"] as? String,
      output("/bin/ps", ["-p", String(pid), "-o", "lstart="]) == stamp,
      let command = output("/bin/ps", ["-ww", "-p", String(pid), "-o", "command="])
    else { return nil }
    // ps prints argv without quoting; split only the exact configured executable.
    let executables = Set([executable, URL(fileURLWithPath: executable).resolvingSymlinksInPath().path])
    guard let interpreter = executables.first(where: { command.hasPrefix($0 + " ") }) else { return nil }
    var argument = String(command.dropFirst(interpreter.count)).trimmingCharacters(in: .whitespaces)
    let flags: Set<String> = ["-u", "-B", "-E", "-I", "-O", "-OO", "-s", "-S"]
    while let separator = argument.firstIndex(of: " "), flags.contains(String(argument[..<separator])) {
      argument = String(argument[separator...]).trimmingCharacters(in: .whitespaces)
    }
    if argument == script || argument.hasPrefix(script + " ") { return Int32(pid) }
    let relative =
      script.hasPrefix(workingDirectory + "/")
      ? String(script.dropFirst(workingDirectory.count + 1)) : ""
    guard !relative.isEmpty,
      [relative, "./" + relative].contains(where: { argument == $0 || argument.hasPrefix($0 + " ") }
      ),
      let cwd = output("/usr/sbin/lsof", ["-a", "-p", String(pid), "-d", "cwd", "-Fn"]),
      cwd.components(separatedBy: "\n").contains("n" + workingDirectory)
    else { return nil }
    return Int32(pid)
  }
  static func verified(
    port: UInt16, child: Process, state: URL?, script: String?, workingDirectory: String
  ) -> Bool {
    let expected: Int32
    if let state = state, let script = script {
      guard let executable = child.executableURL?.path,
        let pid = managedPID(
          state: state, script: script, executable: executable, workingDirectory: workingDirectory, port: port)
      else { return false }
      expected = pid
    } else {
      guard child.isRunning else { return false }
      expected = child.processIdentifier
    }
    guard
      let listeners = output(
        "/usr/sbin/lsof", ["-nP", "-iTCP@127.0.0.1:\(port)", "-sTCP:LISTEN", "-Fp"])
    else { return false }
    let owners = listeners.components(separatedBy: "\n").compactMap {
      $0.hasPrefix("p") ? Int32($0.dropFirst()) : nil
    }
    guard !owners.isEmpty else { return false }
    return owners.allSatisfy { owner in
      var pid = owner
      for _ in 0..<5 {
        if pid == expected { return true }
        guard state != nil, let raw = output("/bin/ps", ["-p", String(pid), "-o", "ppid="]),
          let parent = Int32(raw), parent > 1, parent != pid
        else { return false }
        pid = parent
      }
      return false
    }
  }
}
