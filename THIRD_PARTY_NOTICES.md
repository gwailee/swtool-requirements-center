# 第三方许可来源

本文件记录分发资源对应的官方固定版本许可。原始 vendor 资源中的版权注释和 notice 不作删改；完整许可证保存在以下文件。

| 组件 | 使用位置 | 官方固定版本许可 | 本地许可 |
| --- | --- | --- | --- |
| Mermaid 10.9.1 | `web/vendor/mermaid.min.js` | [MIT](https://github.com/mermaid-js/mermaid/blob/v10.9.1/LICENSE) | `web/vendor/mermaid/LICENSE` |
| KaTeX 0.16.9 | Mermaid bundle 内嵌 | [MIT](https://github.com/KaTeX/KaTeX/blob/v0.16.9/LICENSE) | `web/vendor/mermaid-katex/LICENSE` |
| DOMPurify 3.0.9 | Mermaid bundle 内嵌 | [Apache-2.0 OR MPL-2.0](https://github.com/cure53/DOMPurify/blob/3.0.9/LICENSE) | `web/vendor/dompurify/LICENSE` |
| js-yaml 4.1.0 | Mermaid bundle 内嵌 | [MIT](https://github.com/nodeca/js-yaml/blob/4.1.0/LICENSE) | `web/vendor/js-yaml/LICENSE` |
| KaTeX 0.16.22 | 独立公式脚本、样式、字体 | [MIT](https://github.com/KaTeX/KaTeX/blob/v0.16.22/LICENSE) | `web/vendor/katex/LICENSE` |
| @ungap/structured-clone 1.3.3 | 浏览器兼容资源 | [ISC](https://github.com/ungap/structured-clone/blob/v1.3.3/LICENSE) | `web/vendor/structured-clone/LICENSE` |

KaTeX 0.16.22 的现有本地 LICENSE 与上游该版本文本一致；其官方 `fonts` 目录没有额外 LICENSE 文件。这里不添加未经上游确认的字体许可证。资源编码方式不会改变原始许可义务。

本清单补充已识别组件，不替代 bundle 中其余依赖自带的版权与许可声明，也不替代 Python 安装包各自的许可。升级依赖时应重新检查固定版本及分发内容。
