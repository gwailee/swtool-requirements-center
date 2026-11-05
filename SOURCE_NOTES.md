# 源码来源与许可边界

2026 上海商江网络科技有限公司版权所有。开发者：软工云（swtool.com）开发团队；联系邮箱：uqtool@163.com。

本项目由 SWTool 需求中心独立版整理而来。权利人已授权将本项目自有及获得完整开源授权的源代码按 Apache License 2.0 发布。该授权不覆盖第三方组件的版权，不改变第三方原许可，也不表示整个 SWTool 其他模块均采用该许可。

唯一受支持的服务入口是 `standalone:app`。共享服务实现中未由该入口开放的模块，不属于独立版功能承诺。源码发布以经过校验的 manifest 与构建脚本为准，不收集运行数据，也不把目录中的历史嵌套副本作为新的来源。

## 第三方资源

- KaTeX 0.16.22：浏览器公式脚本、样式及配套字体；现有 MIT 许可保存在 `web/vendor/katex/LICENSE`。已核对官方 v0.16.22 根 LICENSE 与此文件逐字节一致；官方 fonts 目录没有另列许可文件，未擅自宣称 OFL。字体的原始文件与内置资源必须保留相关声明。
- Mermaid 10.9.1：离线图示资源 `web/vendor/mermaid.min.js`。其 bundle 包含 KaTeX 0.16.9、DOMPurify 3.0.9 及其他依赖的原始内嵌许可提示，必须保留；已补入的官方许可及固定版本来源见 `THIRD_PARTY_NOTICES.md`。
- `@ungap/structured-clone` 1.3.3：ISC，原许可保存在 `web/vendor/structured-clone/LICENSE`。
- Python 依赖列于 `requirements.txt`；各分发包保留其自身许可，项目许可证不替代这些依赖的许可证。

禁止删除、缩短或覆盖 vendor 中的原始版权、许可和内嵌 notice。新增依赖或升级资源时，应核对上游固定版本、许可证、字体来源和分发义务，并同步 NOTICE 与源码 manifest。
