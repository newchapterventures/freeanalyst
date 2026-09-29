// FreeAnalyst 的 OCR 层 —— 用 macOS 内置 Vision 框架。
//
// 为什么用它：免费、离线（材料不出本机）、零模型下载、原生支持简繁中文。
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
//: 语言纠正开关（第 6 个参数）。**默认开 = 与改动前完全一致**，
//: 免得 A/B 还没跑就先改了行为；实测之后再定默认值。
let useCorrection = (args.count > 5 ? args[5] : "1") == "1"
//: 置信度下限：低于它的**数值格**会被打上 `？` 标记（第 7 个参数，默认 0 = 关闭）。
//:
//: 为什么用 `？` 前缀而不是别的方式：这个项目里"可疑金额带 ？"已经是既有约定，
//: 下游 `parse_amount` 见到 `？` 会返回 suspect —— 一个字都不用改就能接上。
//: 阈值先给 0（不改变现有行为），等 A/B 实测出置信度分布再定。
let lowConf = args.count > 6 ? (Double(args[6]) ?? 0.0) : 0.0

/// 这一格看着是不是**数值**（金额/比例/编号）。
/// 只要含数字、且不含中日韩文字，就当数值格 —— 标签不受影响，
/// 因为我们**只给数值格**打标记。
func looksNumeric(_ s: String) -> Bool {
    let hasDigit = s.range(of: "[0-9]", options: .regularExpression) != nil
    let hasCJK = s.range(of: "[\\u{4E00}-\\u{9FFF}\\u{3000}-\\u{303F}]",
                         options: .regularExpression) != nil
    return hasDigit && !hasCJK
}

//: 数值格**再认一遍**（第 8 个参数，**默认 0 = 关闭** —— 实测零收益）。
//:
//: 第一遍看的是整页：格子挨得近时，Vision 会把相邻栏的数字并进一格，或者把长数字
//: 中间的几位吞掉 —— 而**吞掉之后形状仍然合法**（`1,234,567,890.12` → `1,234,567.12`），
//: 任何形状校验都抓不到。实测某 104 页扫描件的折旧摊销就是这么错的（差三个量级）。
//:
//: 第二遍把这一格**单独裁出来、放大、只用数字设置**再认一次：
//:   · 只看这一格 → 没有邻栏干扰
//:   · recognitionLanguages 只留 en-US → 不再把负号认成汉字「一」（实测会出现）
//:   · 关语言纠正 → 不按语言习惯猜数字
//:
//: **但实测下来它没用**：那份材料第 72-73 页试了 72 格、认出来 62 格、
//: **与第一遍不一致 0 格** —— 被读错的金额，两遍读的是同一个错。
//: 原因也量清楚了：那份扫描件原始就是 192 dpi，而我们按 3.0 倍渲染 = 216 dpi，
//: **早就在原始像素之上**，放大只是插值、没有新信息。
//: 所以默认关闭；留着是为了换一份材料（原生矢量页）能再量一遍 —— 没有实测支持就不开。
let secondPass = (args.count > 7 ? args[7] : "0") == "1"

//: 裁剪时向外扩一点边（归一化）。贴着字形裁会把笔画切掉，反而认不出。
let cropPad: CGFloat = 0.004

func recognizeNumber(in image: CGImage, box: CGRect) -> (String, Double)? {
    let W = CGFloat(image.width), H = CGFloat(image.height)
    // Vision 的 boundingBox 原点在**左下**，CGImage.cropping 的原点在**左上** → 翻 y
    let x0 = max(0, (box.minX - cropPad) * W)
    let y0 = max(0, (1.0 - box.maxY - cropPad) * H)
    let w = min(W - x0, (box.width + cropPad * 2) * W)
    let h = min(H - y0, (box.height + cropPad * 2) * H)
    guard w > 3, h > 3, let crop = image.cropping(
        to: CGRect(x: x0, y: y0, width: w, height: h)) else { return nil }
    // 放大 3 倍再认：扫描件上的密集数字，放大后笔画分得开
    let up = 3, uw = Int(w) * up, uh = Int(h) * up
    guard uw > 4, uh > 4,
          let ctx = CGContext(data: nil, width: uw, height: uh, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue) else {
        return nil
    }
    ctx.interpolationQuality = .high
    ctx.draw(crop, in: CGRect(x: 0, y: 0, width: uw, height: uh))
    guard let big = ctx.makeImage() else { return nil }

    let r = VNRecognizeTextRequest()
    r.recognitionLevel = .accurate
    r.recognitionLanguages = ["en-US"]      // 纯数字，不让中文规则来猜
    r.usesLanguageCorrection = false
    r.minimumTextHeight = 0.0
    let hd = VNImageRequestHandler(cgImage: big, options: [:])
    do { try hd.perform([r]) } catch { return nil }
    guard let o = (r.results ?? []).first, let c = o.topCandidates(1).first else { return nil }
    return (c.string, Double(c.confidence))
}

/// 比对两遍读法时把"格式差异"抹平：`1,234.50` 与 `1234.50` 算一致。
/// 只比内容，不比千分位/空格 —— 否则每格都会"不一致"。
func sameNumber(_ a: String, _ b: String) -> Bool {
    func norm(_ s: String) -> String {
        s.replacingOccurrences(of: ",", with: "")
         .replacingOccurrences(of: " ", with: "")
         .replacingOccurrences(of: "，", with: "")
         .trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return norm(a) == norm(b)
}

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
    //: Vision 自己的置信度（0~1）。**别丢** —— 低置信度的金额要能被标出来，
    //: 我们自己那套"形状校验"只能抓格式不对，抓不到"数字被换了一个"。
    let conf: Double
}

var outPages: [[String: Any]] = []
//: 二次识别的计数（只写到 stderr）：**不做这一步就分不清**
//: “二次识别没用”和“二次识别根本没跑” —— 后者是假阴性，会骗我们放弃一条有效的路。
var secondTried = 0, secondOk = 0, secondDiff = 0

for pageNo in first...last {
    guard let page = doc.page(at: pageNo - 1), let img = renderPage(page) else { continue }

    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    // **语言纠正对数字是有害的**：它是按语言习惯猜字的，能把 `1,234,567.89`
    // 这种长数字里的数字悄悄换掉（我们实测过：折旧摊销金额比真值小了三个量级）。
    // 但中文科目名**需要**它。所以做成参数：默认开（保标签），读金额时可以关。
    req.usesLanguageCorrection = useCorrection

    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform([req]) } catch { continue }

    var frags: [Frag] = []
    for obs in (req.results ?? []) {
        guard let c = obs.topCandidates(1).first else { continue }
        let bb = obs.boundingBox                      // 左下原点，0..1
        // **低置信度的数值格打 `？`**：Vision 自己就知道它没把握，
        // 而我们那套"形状校验"只能抓格式不对、抓不到"数字被换了一个"。
        // 标上之后，下游会把它当可疑值处理（拒绝猜、并在报告里标出来）。
        var text = c.string
        if lowConf > 0, Double(c.confidence) < lowConf, looksNumeric(text) {
            text = "？" + text
        }
        // **数值格再认一遍**：单独裁出来放大、只用数字设置。
        // 一致就照第一遍（不折腾格式）；不一致就采纳第二遍并打 `？` → 下游当可疑值。
        if secondPass, looksNumeric(text) {
            secondTried += 1
            if let (again, _) = recognizeNumber(in: img, box: bb) {
                secondOk += 1
                if !sameNumber(again, text) {
                    text = "？" + again
                    secondDiff += 1
                }
            }
        }
        frags.append(Frag(text: text, x: bb.minX,
                          y: 1.0 - (bb.minY + bb.height / 2),   // 换成左上原点
                          conf: Double(c.confidence)))
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
            ["x": $0.x, "y": $0.y, "t": $0.text, "c": $0.conf]
        }]
    }
    outPages.append(["page": pageNo, "rows": rows])
}

let payload: [String: Any] = ["pages": outPages]
if secondPass {
    FileHandle.standardError.write(
        "二次识别: 试了 \(secondTried) 格 · 认出来 \(secondOk) · 与第一遍不一致 \(secondDiff)\n"
            .data(using: .utf8)!)
}
if let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
   let text = String(data: data, encoding: .utf8) {
    print(text)
} else {
    FileHandle.standardError.write("JSON 序列化失败\n".data(using: .utf8)!)
    exit(1)
}
