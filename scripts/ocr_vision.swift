// ocr_vision.swift — 用 macOS Vision 框架做 OCR（本地、免费、无需 API）
//
// 编译：swiftc -O ocr_vision.swift -o ocr_vision
// 用法：./ocr_vision img1.png [img2.png ...]           # JSON 输出到 stdout
//       ./ocr_vision --text img.png                    # 人类可读输出
//
// 输出坐标：Vision 的 boundingBox 是归一化坐标（0-1），原点在**左下角**。
// 转成 PDF 坐标（原点左上）需：pdf_y = (1 - y - h) * page_height。

import Foundation
import Vision
import AppKit

func ocr(url: URL) -> [[String: Any]] {
    guard let img = NSImage(contentsOf: url),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write("cannot load \(url.path)\n".data(using: .utf8)!)
        return []
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["en-US", "zh-Hans"]
    req.usesLanguageCorrection = false          // 图表里数字/单位多，纠正反而有害
    req.minimumTextHeight = 0.004               // 允许识别小字（图例、脚注）
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch {
        FileHandle.standardError.write("ocr failed \(url.path): \(error)\n".data(using: .utf8)!)
        return []
    }
    var out: [[String: Any]] = []
    for obs in (req.results ?? []) {
        guard let top = obs.topCandidates(1).first else { continue }
        let bb = obs.boundingBox
        out.append([
            "file": url.lastPathComponent,
            "text": top.string,
            "conf": (Double(top.confidence) * 1000).rounded() / 1000,
            "x": (Double(bb.minX) * 1e5).rounded() / 1e5,
            "y": (Double(bb.minY) * 1e5).rounded() / 1e5,
            "w": (Double(bb.width) * 1e5).rounded() / 1e5,
            "h": (Double(bb.height) * 1e5).rounded() / 1e5
        ])
    }
    return out
}

let args = Array(CommandLine.arguments.dropFirst())
let textMode = args.contains("--text")
let paths = args.filter { !$0.hasPrefix("--") }
guard !paths.isEmpty else {
    FileHandle.standardError.write("usage: ocr_vision [--text] <image>...\n".data(using: .utf8)!)
    exit(2)
}

var results: [[String: Any]] = []
for p in paths { results.append(contentsOf: ocr(url: URL(fileURLWithPath: p))) }

if textMode {
    var current = ""
    for r in results {
        let f = r["file"] as! String
        if f != current { current = f; print("\n### \(f)") }
        print(String(format: "  [%.2f] %@", r["conf"] as! Double, r["text"] as! String))
    }
} else {
    let data = try! JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
}
