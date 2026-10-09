// Embervoice desktop shell (native macOS launcher).
//
// Pure system Swift/AppKit/Foundation. Owns exactly one child process: the
// PyInstaller helper at Contents/Resources/runtime/embervoice-server. The helper
// prints "Embervoice is starting at http://127.0.0.1:<port>/" on stdout before
// the server is ready, so this shell parses that line, waits until
// /api/setup really answers, and only then opens the browser (once).
//
// Privacy: helper output stays on this Mac and is only ever shown in the local
// window. Nothing is uploaded and book text is never printed here.

import AppKit
import Foundation

// MARK: - Configuration

enum ShellConfig {
    static let startupLinePrefix = "Embervoice is starting at "
    static let readyPath = "/api/setup"
    static let gracefulShutdownSeconds: Double = 5.0
    static let readyTimeout: Double = 120.0

    /// Locate the bundled helper. EMBERVOICE_HELPER exists purely so automated
    /// smoke tests can point the shell at a synthetic helper.
    static func helperPath() -> String? {
        let env = ProcessInfo.processInfo.environment
        if let override = env["EMBERVOICE_HELPER"], FileManager.default.isExecutableFile(atPath: override) {
            return override
        }
        let candidate = (Bundle.main.bundlePath as NSString)
            .appendingPathComponent("Contents/Resources/runtime/embervoice-server")
        return FileManager.default.isExecutableFile(atPath: candidate) ? candidate : nil
    }
}

// MARK: - Status

enum ShellStatus: Equatable {
    case starting
    case running(url: URL)
    case failed(message: String)
}

// MARK: - CLI

struct ShellOptions {
    var openBrowserAutomatically = true
    var storePath: String?
    var statusFilePath: String?
    var quitWhenReady = false

    static func parse(_ arguments: [String]) -> ShellOptions {
        var options = ShellOptions()
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--no-browser":
                options.openBrowserAutomatically = false
            case "--quit-when-ready":
                options.quitWhenReady = true
            case "--status-file":
                index += 1
                if index < arguments.count { options.statusFilePath = arguments[index] }
            case "--store":
                index += 1
                if index < arguments.count { options.storePath = arguments[index] }
            default:
                break  // ignore unknown flags rather than failing to launch
            }
            index += 1
        }
        return options
    }
}

// MARK: - Helper process

final class HelperProcess {
    private(set) var process: Process?
    private var stdoutPipe: Pipe?
    private var stdoutBuffer = Data()
    private let stdoutLock = NSLock()

    var isRunning: Bool { process?.isRunning ?? false }
    var pid: pid_t? { process?.processIdentifier }

    /// Launch the owned helper. Returns an error string on failure.
    func start(path: String, storePath: String?, onExit: @escaping (Int32) -> Void, onOutput: @escaping (String) -> Void) -> String? {
        guard FileManager.default.isExecutableFile(atPath: path) else {
            return "The Embervoice helper could not be found at\n\(path)"
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.currentDirectoryURL = URL(fileURLWithPath: (path as NSString).deletingLastPathComponent)

        var arguments = ["--no-browser", "--port", "0"]
        if let storePath = storePath {
            arguments += ["--store", storePath]
        }
        task.arguments = arguments

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice  // keep UI noise down; no log file of book text
        task.standardInput = FileHandle.nullDevice

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard let self = self, !data.isEmpty else { handle.readabilityHandler = nil; return }
            self.stdoutLock.lock()
            self.stdoutBuffer.append(data)
            var lines: [String] = []
            while let end = self.stdoutBuffer.firstIndex(of: 10) {
                let line = String(decoding: self.stdoutBuffer.prefix(upTo: end), as: UTF8.self)
                self.stdoutBuffer.removeSubrange(...end)
                lines.append(line)
            }
            if self.stdoutBuffer.count > 65536 { self.stdoutBuffer.removeAll() }
            self.stdoutLock.unlock()
            for line in lines { onOutput(line) }
        }
        task.terminationHandler = { task in onExit(task.terminationStatus) }

        do {
            try task.run()
        } catch {
            return "The Embervoice helper could not be started:\n\(error.localizedDescription)"
        }
        process = task
        stdoutPipe = pipe
        return nil
    }

    /// SIGTERM, wait up to 5s, then SIGKILL this child only. Never touches
    /// other processes and never restarts the helper.
    func terminateGracefully(timeout: Double = ShellConfig.gracefulShutdownSeconds) {
        guard let task = process, task.isRunning else { return }
        kill(task.processIdentifier, SIGTERM)
        let deadline = Date().addingTimeInterval(timeout)
        while task.isRunning && Date() < deadline {
            usleep(100_000)
        }
        if task.isRunning {
            kill(task.processIdentifier, SIGKILL)
            task.waitUntilExit()
        }
    }
}

// MARK: - Readiness probing

enum ReadinessProbe {
    /// A server "answers" on any HTTP status (including 4xx/5xx): the point is
    /// that the loopback socket is serving, not what it says about setup.
    static func answers(_ url: URL, timeout: TimeInterval = 2.0, completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        let task = URLSession.shared.dataTask(with: request) { _, response, _ in
            completion((response as? HTTPURLResponse) != nil)
        }
        task.resume()
    }
}

// MARK: - UI

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let options: ShellOptions
    private let helper = HelperProcess()
    private var didOpenBrowser = false
    private var isQuitting = false
    private var statusFileHandle: FileHandle?
    private var terminationSources: [DispatchSourceSignal] = []

    private var window: NSWindow!
    private var statusLabel: NSTextField!
    private var detailLabel: NSTextField!
    private var openButton: NSButton!

    init(options: ShellOptions) {
        self.options = options
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        installTerminationSignalSources()
        startHelper()
    }

    // MARK: Window

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 220),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Embervoice"
        window.isReleasedWhenClosed = false

        let title = NSTextField(labelWithString: "Embervoice")
        title.font = .systemFont(ofSize: 20, weight: .semibold)

        statusLabel = NSTextField(labelWithString: "Starting…")
        statusLabel.font = .systemFont(ofSize: 13)
        statusLabel.textColor = .secondaryLabelColor

        detailLabel = NSTextField(wrappingLabelWithString: "Launching the local Embervoice server. This stays on your Mac.")
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 4
        detailLabel.isSelectable = true

        openButton = NSButton(title: "Open Embervoice", target: self, action: #selector(openEmbervoice))
        openButton.keyEquivalent = "\r"
        openButton.isEnabled = false

        let quitButton = NSButton(title: "Quit", target: self, action: #selector(quitApp))
        quitButton.keyEquivalent = "q"

        let buttons = NSStackView(views: [openButton, quitButton])
        buttons.orientation = .horizontal
        buttons.spacing = 12

        let stack = NSStackView(views: [title, statusLabel, detailLabel, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 220))
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 28),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -24)
        ])
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: Lifecycle

    private func startHelper() {
        apply(status: .starting, detail: "Launching the local Embervoice server…")
        guard let path = ShellConfig.helperPath() else {
            fail("The bundled Embervoice helper is missing from this app.")
            return
        }
        let failure: String? = helper.start(path: path, storePath: options.storePath, onExit: { [weak self] code in
            DispatchQueue.main.async {
                guard let self = self, !self.isQuitting else { return }
                if case .failed = self.currentStatus { return }
                self.fail("The local server stopped (exit \(code)). Quit and reopen Embervoice, or download a fresh complete copy.")
            }
        }) { [weak self] line in
            DispatchQueue.main.async { self?.handleHelperLine(line) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + ShellConfig.readyTimeout) { [weak self] in
            guard let self = self, !self.isQuitting, self.currentStatus == .starting else { return }
            self.fail("The local server did not become ready in time. Quit and reopen Embervoice.")
            self.helper.terminateGracefully()
        }
        if let failure = failure {
            fail(failure)
        }
    }

    private func handleHelperLine(_ line: String) {
        guard line.hasPrefix(ShellConfig.startupLinePrefix) else { return }
        let urlString = String(line.dropFirst(ShellConfig.startupLinePrefix.count))
        guard let url = URL(string: urlString), url.scheme == "http", url.host == "127.0.0.1", let port = url.port, port > 0 else { return }
        waitUntilReady(baseURL: url, port: port)
    }

    private func waitUntilReady(baseURL: URL, port: Int) {
        let deadline = Date().addingTimeInterval(ShellConfig.readyTimeout)
        var attempts = 0
        func poll() {
            if !helper.isRunning {
                DispatchQueue.main.async { self.fail("The Embervoice helper stopped before it was ready.") }
                return
            }
            guard let probeURL = URL(string: "http://127.0.0.1:\(port)\(ShellConfig.readyPath)") else { return }
            ReadinessProbe.answers(probeURL) { ok in
                DispatchQueue.main.async {
                    if ok {
                        self.markRunning(baseURL)
                        return
                    }
                    attempts += 1
                    if Date() >= deadline {
                        self.fail("The Embervoice server did not become ready in time.")
                        return
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { poll() }
                }
            }
        }
        poll()
    }

    private func markRunning(_ url: URL) {
        let target = url.appendingPathComponent("")
        apply(status: .running(url: target),
              detail: "Ready at \(target.absoluteString)\nEverything stays on this Mac.")
        openButton.isEnabled = true
        if options.openBrowserAutomatically {
            openInBrowser(target)
        }
        if options.quitWhenReady {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.quit() }
        }
    }

    private func fail(_ message: String) {
        apply(status: .failed(message: message),
              detail: "Embervoice could not start.\n\n\(message)\n\nNothing was retried automatically.")
        openButton.isEnabled = false
    }

    private func openInBrowser(_ url: URL) {
        guard !didOpenBrowser else { return }
        didOpenBrowser = true
        NSWorkspace.shared.open(url)
    }

    @objc private func openEmbervoice() {
        if case .running(let url) = currentStatus {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func quitApp() { quit() }

    private var currentStatus: ShellStatus = .starting

    private func apply(status: ShellStatus, detail: String) {
        currentStatus = status
        switch status {
        case .starting:
            statusLabel.stringValue = "Starting…"
        case .running:
            statusLabel.stringValue = "Running"
        case .failed:
            statusLabel.stringValue = "Error"
        }
        detailLabel.stringValue = detail
        writeStatusFileIfNeeded()
    }

    /// --status-file is a smoke-test affordance: records transitions locally.
    private func writeStatusFileIfNeeded() {
        guard let path = options.statusFilePath else { return }
        if statusFileHandle == nil {
            FileManager.default.createFile(atPath: path, contents: nil)
            statusFileHandle = FileHandle(forWritingAtPath: path)
            statusFileHandle?.truncateFile(atOffset: 0)
        }
        let line: String
        switch currentStatus {
        case .starting: line = "starting\n"
        case .running(let url): line = "running \(url.absoluteString)\n"
        case .failed(let message):
            let safe = message.replacingOccurrences(of: "\n", with: " ")
            line = "failed \(safe)\n"
        }
        statusFileHandle?.write(line.data(using: .utf8) ?? Data())
    }

    func quit() {
        isQuitting = true
        helper.terminateGracefully()
        NSApp.terminate(nil)
    }

    /// Any other exit path (including a SIGTERM to the app) still reaps the
    /// owned helper instead of orphaning it.
    func applicationWillTerminate(_ notification: Notification) {
        isQuitting = true
        helper.terminateGracefully()
    }

    /// Turn SIGTERM/SIGINT into an orderly app quit so the helper is reaped.
    func installTerminationSignalSources() {
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in self?.quit() }
            source.resume()
            terminationSources.append(source)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

// MARK: - Menu

func installMainMenu() {
    let mainMenu = NSMenu()
    let appItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "Quit Embervoice", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    mainMenu.addItem(appItem)
    NSApp.mainMenu = mainMenu
}

// MARK: - Entry point

let options = ShellOptions.parse(Array(CommandLine.arguments.dropFirst()))
let application = NSApplication.shared
let delegate = AppDelegate(options: options)
application.delegate = delegate
installMainMenu()
application.setActivationPolicy(.regular)
application.run()