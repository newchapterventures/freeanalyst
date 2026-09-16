// FreeAnalyst 的 OCR 层 —— 用 macOS 内置 Vision 框架。
//
// 为什么用它：免费、离线（零出境）、零模型下载、原生支持简繁中文。
// 代价：只能在 macOS 上跑。
//
// 用法： swift ingest/vision_ocr.swift <pdf路径> [起始页] [结束页] [倍率]
// 输出： stdout 上一行 JSON —— {"pages":[{"page":1,"rows":[{"cells":[...]}]}]}
//
// ## 为什么必须做「坐标配对」
//
// Vision 把表格的**左列（科目名）和右列（金额）当成两个独立文本块**返回，
// 直接顺序打印会变成「先全部科目名、再全部金额」—— 完全没法用。
// 实测：某资产负债表 105 个文本块，顺序输出是 1-76 行标签、77-133 行数字。
//
// 所以这里按 boundingBox 的 y 坐标把文本块聚成「行」，行内按 x 排序，
// 还原出真正的表格结构。这是 OCR 能不能用于财务表的关键一步。

import Foundation
import PDFKit
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("用法: vision_ocr.swift <pdf> [起页] [止页] [倍率]\n".data(using: .utf8)!)
    exit(2)
}
let pdfPath = args[1]
let startPage = args.count > 2 ? (Int(args[2]) ?? 1) : 1
let endPage = args.count > 3 ? (Int(args[3]) ?? 0) : 0
let scale = CGFloat(Double(args.count > 4 ? args[4] : "3.0") ?? 3.0)

guard let doc = PDFDocument(url: URL(fileURLWithPath: pdfPath)) else {
    FileHandle.standardError.write("打不开 PDF\n".data(using: .utf8)!)
    exit(1)
}

let last = endPage > 0 ? min(endPage, doc.pageCount) : doc.pageCount
let first = max(1, startPage)

/// 同一行的 y 容差（归一化坐标）。太小会把一行拆开，太大会把相邻行并到一起。
let rowTolerance: CGFloat = 0.006

func renderPage(_ page: PDFPage) -> CGImage? {
    let rect = page.bounds(for: .mediaBox)
    let w = Int(rect.width * scale), h = Int(rect.height * scale)
    guard w > 0, h > 0,
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue) else {
        return nil
    }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

struct Frag {
    let text: String
    let x: CGFloat
    let y: CGFloat
}

var outPages: [[String: Any]] = []

for pageNo in first...last {
    guard let page = doc.page(at: pageNo - 1), let img = renderPage(page) else { continue }

    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    req.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform([req]) } catch { continue }

    var frags: [Frag] = []
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first else { continue }
        let bb = obs.boundingBox                      // 左下原点，0..1
        frags.append(Frag(text: c.string, x: bb.minX,
                          y: 1.0 - (bb.minY + bb.height / 2)))   // 换成左上原点
    }
    frags.sort { $0.y < $1.y }

    // 按 y 聚类成行
    var grouped: [[Frag]] = []
    for f in frags {
        if var lastRow = grouped.last, let anchor = lastRow.first, abs(anchor.y - f.y) < rowTolerance {
            lastRow.append(f)
            grouped[grouped.count - 1] = lastRow
        } else {
            grouped.append([f])
        }
    }

    let rows: [[String: Any]] = grouped.map { row in
        // **带上 x 坐标。** 只靠「数字出现的先后」猜列是不行的：
        // OCR 有时认出「行次」列、有时漏掉，于是同一个金额在不同行
        // 会落到不同栏 —— 在财务表上这是不可接受的静默错误。
        // 上层用 x 聚类成列，才对得上。
        ["cells": row.sorted { $0.x < $1.x }.map {
            ["x": $0.x, "y": $0.y, "t": $0.text]
        }]
    }
    outPages.append(["page": pageNo, "rows": rows])
}

let payload: [String: Any] = ["pages": outPages]
if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
   let text = String(data: data, encoding: .utf8) {
    print(text)
} else {
    FileHandle.standardError.write("JSON 序列化失败\n".data(using: .utf8)!)
    exit(1)
}
