# Edge plan phase 1 - STATUS: COMPLETE (2026-10-18 ~09:50 EDT). Resumed after the 08:25 boot; post_boot_check PASS.
Everything under "Pending" below was done; see ops/edge_harness_README.md for the verification sheet.
Post-resume changes: survivorship coverage recount (strict rule) + gap fill (EODHD total 257 requests);
harness tier order sp_exits-first; pre-registered H4 PIT calibration (FAILED its criterion, recorded as is).
Phase 2 is NOT started: no H1 prereg, no validation/holdout reads, no Alpaca trades.

---- original pause record below ----
# Edge plan phase 1 - RESUME FILE (paused 2026-10-18 ~08:05 EDT for hardware shutdown)

On resume: run `bash /data/finance/ops/checks/post_boot_check.sh` first (expect the 12 user timers to be back via enable-at-boot), then continue from 'Pending' below.

## Done

- Downloads complete (ALLDONE in logs/edge_download.run.log). Nothing in flight; no .part files. MANIFEST.json written for each source (sha256, bytes, zip_ok, per-member rows):
  - raw/dera_insider: 83/83 files, zip_ok 83, 0.93 GB
  - raw/dera_fsds: 71/71 files, zip_ok 71, 5.71 GB
  - raw/ff: 2/2 files, zip_ok 2, 0.00 GB
- Survivorship: raw/price/sp_exits/ (EODHD tier). Coverage 270/276 exit tickers (was 118); missing ARNC BHGE COV RE SE WYND; 176 EODHD requests logged (raw/price/sp_exits/_meta/requests.log). Budget left within approved ~300: ~124.
- Code (WIP commit): scripts/edge_download.py, survivorship_fill.py, fold_gate.py, edge_harness.py, edge_events.py, edge_calibrate.py.
  - Fixes since first draft: pass_bar now counts missing robustness reruns as FAIL (a placebo draw passed before this fix); DSR N = trials of the same kind/name family; placebo now draws point-in-time S&P members (harness null); H4 events use event_study.load_and_detect's exact rule (685 events 2015-2019).
- Results so far (dev fold 2015-2019 only; logs/edge/calibration_summary.json):
  - PIT placebo, 20 draws: mean abn -0.03%, mean t -0.22, |t|>2 share 0.00, no draw passes the bar -> placebo_pass TRUE.
  - Ledger-ticker placebo (diagnostic, pre-fix run): +1.09% / 63d vs PIT EW universe, mean t 2.26 -> the 499-name ledger universe is survivor-biased.
  - H4 vs PIT EW universe: +1.82% gross abn (t +2.50), FF alpha t +0.59 -> the criterion first written (abn vs EW < 0, t<=-2) FAILS.
  - H4 within-name (event_study method, next-open entry): -1.62% mean / -1.02% median, month-clustered t -0.67; by year 2015 -8.3, 2016 +5.3, 2017 +3.1, 2018 -2.2, 2019 -4.2 (%), matching event_study's era table (2015 -10.2, 2016 +6.0). Sign reproduced, not significant on 2015-2019. The within-name comparison was added AFTER seeing the first result - label it as such.
  - variants.jsonl note: the first (ledger-ticker) placebo batch was logged under name placeboNN, so the PIT placebo's DSR counted those 20 too (append-only log, not edited).
- Shutdown prep: no finance service active; 12 user timers STOPPED (still enabled); LanceDB filings count_rows 49,779,626; zfs-replicate last success 08:00:03 EDT.

## Pending (phase 1)

- ops/checks/check_edge_downloads.py (manifests: 83/71/2, zip_ok, sha256 re-hash) and check_survivorship.py (coverage.json, 6 missing, requests<=300).
- ops/checks/check_edge.py: recompute trades independently from price files (verify price_sha), fills strictly after event (next open), costs, SPY/EW bench (sample), event stats, independent NW alpha, DSR from variants.jsonl, gate token / holdout ordering; fail on tamper.
- tests/test_edge_harness.py + tests/test_fold_gate.py (synthetic look-ahead, window cut, costs/2x, delist -30/-100, per-date bench, NW known alpha, DSR N=1==PSR, fold_gate temp repo, planted effect, placebo, check_edge tamper); throttle-coverage test must pass; scripts/run_tests.sh all green.
- Fix scripts/lang_shift_outcomes.py to per-date benchmark via harness + dev-only fold_gate window. DO NOT run it.
- ops/edge_harness_README.md (1 page for Verifier Bot); calibration criterion text in edge_calibrate.py docstring must be updated honestly (original vs within-name).
- Final commit (local only) + final report. Then STOP: no H1 prereg, no validation/holdout reads, no Alpaca trades.

## Download sha256 (all files)

### raw/dera_insider

- 2006q1_form345.zip  62becdadbe5eaff68f03edefe2ba2357c8bb498a1f825b697003e087cf98e6ce  17306804  zip_ok=True
- 2006q2_form345.zip  a787efec9ff37f2034b05b9aeefdba3f1d81c2a063749facfe3d62fc73b2ba14  14116730  zip_ok=True
- 2006q3_form345.zip  ab82fad621ca5e2d682f0c3510b73947bb484c57e58acfed1d5756054cc20e3f  11083649  zip_ok=True
- 2006q4_form345.zip  b5c82cdec683f5342409a462500f372ed9b83c8bb2652fafcc952cc0ead5cd9e  13180511  zip_ok=True
- 2007q1_form345.zip  de4b7259247e3b99d9bab65fc5d9d980a03daf59b3883231792d15e1a566e9f8  17151240  zip_ok=True
- 2007q2_form345.zip  cf8c84c69ae3153262b3981716f0c7d974f229fb3b4606e4c6d32dca823bc520  14744238  zip_ok=True
- 2007q3_form345.zip  7d635af4f1dd591e164d113c80e8a7d9d5613bdb79aab966aa522c68107c0cc0  12700224  zip_ok=True
- 2007q4_form345.zip  1d3040fe4d601f3f14ac4e8b0fb6f6944fd3c6e1c088f7a5648393dcfb6ac7e9  13861041  zip_ok=True
- 2008q1_form345.zip  872feae22ded06edc8e3edd364067d51ab54d8a647d5fb48417a06ced19cec2e  16075732  zip_ok=True
- 2008q2_form345.zip  3716ca2bd8be9f4dce67e4cbfb2ae1748a905b838988e1aad4eba650746034c4  13824775  zip_ok=True
- 2008q3_form345.zip  4efecc7ec2985f7b65efcdb9eea55565960d769812fa6685122cc9a66365a616  10862303  zip_ok=True
- 2008q4_form345.zip  d98887013329785788a3cde04a32380b98640091e1edd841f1a9fa14e6b34b34  10836597  zip_ok=True
- 2009q1_form345.zip  a81a4741c49b79c61426dcb9100b10221c76f524ad1c3d9e62716b92a2f2ba2b  13143873  zip_ok=True
- 2009q2_form345.zip  51e552335e82acec61bd7a38a9d16913896f9c6da28e6e47c1f8bf1159276ef8  10091884  zip_ok=True
- 2009q3_form345.zip  a0302d87c82e1ced213ea097ab7d5174589c6b98642e8bb657e86a87247d375d  9012661  zip_ok=True
- 2009q4_form345.zip  4ff26071432bb997fc5124cbd85ab03f8eca7fd469a5706d49c58887155b03a8  9888102  zip_ok=True
- 2010q1_form345.zip  bd7c7eaf3603bcee1a904d89d8bb957367513a1e2f47859dbcbce88359be2849  13765513  zip_ok=True
- 2010q2_form345.zip  1bafd756d517546cc4627424d6be8095e822ceec23dcd462bfb4d7f09d58d3d6  11197221  zip_ok=True
- 2010q3_form345.zip  8ba031550e019f62706f8230fe67aeccd593523397ed22d16ab53de5ad77460d  8833546  zip_ok=True
- 2010q4_form345.zip  1dba15dcac7b22a5caedf9db8ad27da5eb59c1ca398ed3cde11bcc4a25970092  10842841  zip_ok=True
- 2011q1_form345.zip  9cb7a76afdadd0707e503e875bccfa26fdeecea701a9f8edf0a6bab561fe8515  14431321  zip_ok=True
- 2011q2_form345.zip  4c61838de792994f1949730282ca5c8b046190309a510d7d0228a1ea5bf15434  11265081  zip_ok=True
- 2011q3_form345.zip  a173d41967a2c85996b0c3018d0ce5baf18100e7230a93fe7e5a01a759f225ce  8440873  zip_ok=True
- 2011q4_form345.zip  47f35cc68de7c43c7ab7dd86f00a8c9e5f5aa1f4bca0ea859b903d17f198e0b8  8958649  zip_ok=True
- 2012q1_form345.zip  0cac6f943813dae94c20ecc8bca63fda692e2b8ac6d02ce2c322ab411f45c357  14219288  zip_ok=True
- 2012q2_form345.zip  0ac1582986fd5a7486a6e34ab25f11a46fe8082d03f815340f8dda01f34a65e5  10535239  zip_ok=True
- 2012q3_form345.zip  a4f3223313657293318d82008e7c75112e35c5a392378872bd2ee3ddac6d5683  9104710  zip_ok=True
- 2012q4_form345.zip  aa328a6dd671ca9ac4b1746d14f224780a11c2721dd143735e6f01a9f39b502d  10438069  zip_ok=True
- 2013q1_form345.zip  795607b5f0c1510ffa086ca86f3f45abb54a432f49038f4ace16d8aa6ac12756  14032058  zip_ok=True
- 2013q2_form345.zip  0a74fce493cb2b871faeac26a4eca53e663836675a3c8cd45fb39862a6fe9749  11427360  zip_ok=True
- 2013q3_form345.zip  9b4c977f57713c8b8d0701907a9b27bfebf9d430602dd7c4a0b8e83cd91ad7ed  9557012  zip_ok=True
- 2013q4_form345.zip  7fbe8f624cde7507204bff090ee86a95de9533049a845428f72d14721824a13f  10117129  zip_ok=True
- 2014q1_form345.zip  784f13d1c1015b01c3d513915e7f1c50151b25aa4965704ac279041c5fc6f7aa  14560119  zip_ok=True
- 2014q2_form345.zip  54a109f0ed7f94b8bbc2b2a68bfac2aab63ec34e722ddcdc3ddee40703209310  11012390  zip_ok=True
- 2014q3_form345.zip  86c00d07b7056a0f2a124e313180ea064ed145f514e54cc3a8513dbf40a0fa22  8979142  zip_ok=True
- 2014q4_form345.zip  b37ac4d70bc97cff746e20f475c6cc392288e047f2a21d30c007868123793aa7  10294808  zip_ok=True
- 2015q1_form345.zip  36698eefccea88e8737868a43eb6dd2e0c726f2d026bce3dd0fcacdef382dfd6  14689945  zip_ok=True
- 2015q2_form345.zip  e07bc99bf94a1375f6e8df86189f5193ac3f276d388a7646a2ed71bcffe3b539  11336215  zip_ok=True
- 2015q3_form345.zip  f50e2e584aa552e2abc6d0cbb9a2e92217c8e2ec8c3cd79c1103dd5ca2123c4e  8908570  zip_ok=True
- 2015q4_form345.zip  a31dc71e838e9b81e77ff6d528af3be0f1a39b7fcfe0c2486a89dd0a369910fa  9384596  zip_ok=True
- 2016q1_form345.zip  5b3bd7e2bcc3c8468c3b6d3bcb1f8ca77596ca4ea7ccf696682e84d98a6bdc0b  13630106  zip_ok=True
- 2016q2_form345.zip  b99ffbceff8e31d49c6a01bc475ec8c7105c2895632b9eb3d72066db94c252a7  10388337  zip_ok=True
- 2016q3_form345.zip  5a25d3c6cb874875904b2be0059bb4784e4da28b315af30b15568fd250bd0dde  8704557  zip_ok=True
- 2016q4_form345.zip  3fd9daf61e256a9463958dc57f90eb9885c9d9f00e98a2d437346d78991252bd  9107157  zip_ok=True
- 2017q1_form345.zip  e3c135d06aeb13ccb7554e4b89cd4ade40faf9d0d39c8c6a7010b3f3260e5371  14209324  zip_ok=True
- 2017q2_form345.zip  071c75391305e5e5f01db18fd5c954da3800164a15fdad8b1161098fecd63786  10633369  zip_ok=True
- 2017q3_form345.zip  ca64bbc6a5a728c80e9532d91028b5a19bd8f8f8d179344e30e9eb361bb1bd18  7972500  zip_ok=True
- 2017q4_form345.zip  d529e3ebe7d1f4f022580eabb703b25ef66b42199dbb8adfe06e988d99bca06d  9036107  zip_ok=True
- 2018q1_form345.zip  8541f5c8a975ec99bf21964a77593775e02f55200a7fdf4c0711a7c858a9e2fb  13941852  zip_ok=True
- 2018q2_form345.zip  3e0942f34eae067a676b4482e927297216d8adc7449ac27ea8860f643724d05b  10757463  zip_ok=True
- 2018q3_form345.zip  c5c7e1f946b80dd2419356974a717d1e92b875436f7dad4b6b8e2094f867682e  8493817  zip_ok=True
- 2018q4_form345.zip  03dd59b0da4e10e98425a0961d04126707b127b8b8f33e13092b80deed087a9f  9225725  zip_ok=True
- 2019q1_form345.zip  3206a941a818d1210857277b8dff86900df8aa00e10902bfa2fa8879b1011a5d  13745081  zip_ok=True
- 2019q2_form345.zip  0d6d5fb5cc922dee8ca7451156a44ba76c5e1db65b02bb6473fe16354087c308  10594521  zip_ok=True
- 2019q3_form345.zip  d3ae11e56f9ec064e95b074e808bd08c19a7a1320fcfd146708bc80543a963bd  8188905  zip_ok=True
- 2019q4_form345.zip  df28aa75bf703f0c202c59d94046e84e8e082a97a49965a4b66149d3184e540c  8416016  zip_ok=True
- 2020q1_form345.zip  3c3bc3fefa4dbe32cb68f052ac4ea32cae16099bb2b104a0bd8d242465e004a8  13886112  zip_ok=True
- 2020q2_form345.zip  665be773d41b30fae10f4dea03096a0e33a7557b0acbb3fab9f1cd5796ee2b22  10237249  zip_ok=True
- 2020q3_form345.zip  5b89d93d94b70a649355bd47ec5a68a0450e40cc12f082b2817079589aa541b1  8765495  zip_ok=True
- 2020q4_form345.zip  0abb3ec65cb2ef4ef15d0e3f4c7ba782a2801b9998d32b5b36c3f44cc4130af3  10162651  zip_ok=True
- 2021q1_form345.zip  3ea90731d424e9b0324f00a6bab1ad603cc9419d9366025f7d7120fffc2a73c5  15891333  zip_ok=True
- 2021q2_form345.zip  7277d43d94cda0fdb83d7e6f4d9021a3baa2d886a963c0cd97b65e6875aec3b6  12494758  zip_ok=True
- 2021q3_form345.zip  8323d1ceb62ebbdc27da242533501f8abea7ef9703bc374dc28cef7379aa0070  10198876  zip_ok=True
- 2021q4_form345.zip  cd630b3ba9402d578b9ec88d637f022248f1452476de9066b4813823200e3fca  10818822  zip_ok=True
- 2022q1_form345.zip  bc733530b3360ec59b624041043a4b50619feba4cd8a13aea441d171d6d2c2c5  14253893  zip_ok=True
- 2022q2_form345.zip  fc091183d24d25283826e7e11e0318a8027995a9d77ea8bc4667589eb2b4d8f5  10603723  zip_ok=True
- 2022q3_form345.zip  846c1115906b1c45dd8761da076a32ea0cb9f8995dae76f81d0cd93b7e05b032  8024500  zip_ok=True
- 2022q4_form345.zip  6c6a909bd2eaaa0a24ccf5f0071b404670c115845d2939bc592bf3ce420dcdb9  8400983  zip_ok=True
- 2023q1_form345.zip  0b476188f52a0862e71886eb31a81f6345fc6616403b52188484aef0438cfcfb  13882049  zip_ok=True
- 2023q2_form345.zip  7e636787231ae0f00accf5c3627298402dedd1aefffd7c50fb48292e5d3a8fbb  10742628  zip_ok=True
- 2023q3_form345.zip  26947559fbd710ca4fb007e2ff0481b13ebc3ca47153e77a9b0bc84db250a48e  8202487  zip_ok=True
- 2023q4_form345.zip  be19822ad9116ea6c1a55303b1f2e23864f21101e42d2da0455a6ff2b33e1a9e  8497639  zip_ok=True
- 2024q1_form345.zip  8e9bb34335416829a88e4ca7d09bb8aaceca689da9718fef22c298d6f18c9b04  13874620  zip_ok=True
- 2024q2_form345.zip  782abee663e466b243f953625ae319da93c3e896816007d5a375e558c94dc7c1  10286787  zip_ok=True
- 2024q3_form345.zip  8b55b4ffd91e56770fc0e71f9b73e6886f3c60fac8b7266e4b3e5586d2857910  8163472  zip_ok=True
- 2024q4_form345.zip  44763a00aa8a2077747bc015f12c6a4c09ba7cae4a2d031bf24398f9db9e52f3  8954329  zip_ok=True
- 2025q1_form345.zip  37f6b4287df129b88acf0782498540640d055b1c893320877e329945b534a8c2  12804783  zip_ok=True
- 2025q2_form345.zip  64e7f81328933b1281bd8737961048d76d2ab3d5d1d2d7149a32ec061b0e3ff2  9860984  zip_ok=True
- 2025q3_form345.zip  9c57f272b7230500771f661bc1fe181527ed6b76a6f45d23a910c7fc7e9fcfa4  7956025  zip_ok=True
- 2025q4_form345.zip  6af26a68f5834eab96dff2c568530d9213638d3e6c9d1256e4f6c9f8af1d2d28  8309708  zip_ok=True
- 2026q1_form345.zip  3dcb680593f5829bc951cb0969e858b60fc21a6835b094838218093a849592d6  13874904  zip_ok=True
- 2026q2_form345.zip  11f1b2bbbdcbe6347a34437c02d04202fda0eca1dbb023726e4b56504b802e27  11498860  zip_ok=True
- 2026q3_form345.zip  a4e6a681cfcfa43a3d9e4ca9cfec1fd26a59d0a01eac7e9f7b5a0122092c90f3  8660776  zip_ok=True

### raw/dera_fsds

- 2009q1.zip  181327faaa37c2a3b47cb6727004960b762954d908697b252d12bea245b9d26e  13540  zip_ok=True
- 2009q2.zip  09d38a3fdea044be2728488ecb000fef8b5220fda805c601d2337afd1da333a8  144894  zip_ok=True
- 2009q3.zip  dca07de82a7c40d28108cd3ba4fbce4225a9c5bd2bd9deea7167ff4992c8ddfa  3544077  zip_ok=True
- 2009q4.zip  ad23b114ecb430c3a05ee2f2be799e966a67591ef098c44311b2f1f632d3ea82  4050938  zip_ok=True
- 2010q1.zip  2a4db2340ab1662d5135d7e17be1a58352c5959017350f0ef2174135f9996125  5311282  zip_ok=True
- 2010q2.zip  25f416b020fd3e62a67dbf8c6522c9f239e716099b61cf5d05a59a91ffdef1f1  3994236  zip_ok=True
- 2010q3.zip  52014faf614ca0d7b8040e364f5b3e99f28d5e96b711aa922b490056e2c1d7b5  13745950  zip_ok=True
- 2010q4.zip  3dc43a8559cba9e1a9bb5a283a99e1fdfd2b0eaa7a9460887d5b4c70d69cef63  14679781  zip_ok=True
- 2011q1.zip  e63b781a5e57e55d68e110a06d3f697e942a60e17f093bbdc1a16ac7152a7df5  19615544  zip_ok=True
- 2011q2.zip  10a6f98f14d2a341e938a379ea32beb4857b01d59facf04e6e7b3c68558a95d1  15001310  zip_ok=True
- 2011q3.zip  43b9beb243c6686a29df5cf817ed2cb3a7c86a7398bbc56e2fb4d0cf4784ac28  57941919  zip_ok=True
- 2011q4.zip  0f674f68094cc65c83da3c142775dcfd091250e293783c3e8211ee4e97bc083f  64306658  zip_ok=True
- 2012q1.zip  42ed1a5ecac978d4338bd51cd4bc15bf60cc821291eea492f8172fc4e803d5f6  85474601  zip_ok=True
- 2012q2.zip  0dbb10aa316c674f07849a28e67a25a0a59bd6ef2bc214cd1800c8a2d2a05df6  80075771  zip_ok=True
- 2012q3.zip  a5b705fab7948837fc890d820c2b809d4afdb0ff1836129ad5a16127275f0ebf  89608234  zip_ok=True
- 2012q4.zip  af6629de7547f5d75c85abf170550b9466768071fcd419d5fdf1a1d026436e98  95805256  zip_ok=True
- 2013q1.zip  329f4e3e40c0351001037ad1dea3fd28c07100f6f6d6d64d5998ea01b060a790  99173442  zip_ok=True
- 2013q2.zip  7cc764386ea9cb01d62e1a9f4fac38dc9c741e96886a953ddaad2def4eb489c7  96917655  zip_ok=True
- 2013q3.zip  c9134387b650a9194f14b3f832f16aa1a72255c20a0e0b954001a643933cc849  98355005  zip_ok=True
- 2013q4.zip  7781ac25ddbd4f6eb8eb85e838656181fb06e8bc16b1dd5efccb9e21f8f5baa7  92335773  zip_ok=True
- 2014q1.zip  bf55481c6e0088fb45e8efeb2d8de6912a9c3533da918376a24ca5f84144680f  102843522  zip_ok=True
- 2014q2.zip  ab03600231a08369a980a31dde8bb78b6cd49f56d39ab7b90ca5bc0cfd52aa7f  85032839  zip_ok=True
- 2014q3.zip  4d10510a2f17a6da8012d83f66bed7fbb434bca2529f7b2a92043c522d2c52b1  87408142  zip_ok=True
- 2014q4.zip  c82395b7df380a95b30d652784372a5439dea9ed750d89784822bb643cbc4930  87639973  zip_ok=True
- 2015q1.zip  baa9e434480b8299593b781e8156f855e0c4548677329f166f495cbae287e52e  98944359  zip_ok=True
- 2015q2.zip  d3cac325930af6ef145d26fb7e035d7933e900ce174a11adaabb33abd87fbfa1  78908272  zip_ok=True
- 2015q3.zip  9301fa2e346aa6b3a48a36cafab80d1aab5d7970e60bfd168d53ba852a4deca9  82751989  zip_ok=True
- 2015q4.zip  1723b41ef2a1d3ea6595f6052ef229a80e03cd183391ca9e297a894f9e589d83  83007568  zip_ok=True
- 2016q1.zip  5c89a6badede1b47ab540913c7484795c7fc4c2a4672ac601b34b8c0afe5a698  95470843  zip_ok=True
- 2016q2.zip  e91daef1f83ab3a6a7a25425f1f09ac6c100be8e42ac7a54e539e1b0c303449c  73848835  zip_ok=True
- 2016q3.zip  4d014b8ee4476887aba067c59bfef967d4b12c905d4b1b3f89e8ff7ff7310156  71116399  zip_ok=True
- 2016q4.zip  b8a9550c386ea95eefcc95fc3b3c19f2b1428c0a7f46d5d547a671c3f9284e26  79400707  zip_ok=True
- 2017q1.zip  ffb6989983c4c9f87e2f39166b20e4a21f024f0fe1adccc199e08da5fc94a915  92433981  zip_ok=True
- 2017q2.zip  63a590d7deaac8f82d22f08cb2ada46f199341ecc7e7a8a0a7d6a181cec1d53b  71429748  zip_ok=True
- 2017q3.zip  5aa58d799c4fb3f5f98974eb713696d1aebe747a01a794e7ac58517631b5735d  74356472  zip_ok=True
- 2017q4.zip  be03bfd71f356a7f53d82a40ee907bb51aec4f61d6ce97f059f2df785bc37658  76051784  zip_ok=True
- 2018q1.zip  f7f0083d28779ff373cd097ded88d5408eae9cc178f89014ca3ed8b24dc84185  93176693  zip_ok=True
- 2018q2.zip  99daaf8af1fb1ba94b100613536d9dbee5e9a356f0736190963a3a68af623d54  91972487  zip_ok=True
- 2018q3.zip  30030056223c456d5cb7a42722ffa990345fda12deb5cfff723b1ae47dc9bf8d  87202498  zip_ok=True
- 2018q4.zip  ecc103d0979dacdf6cfcc7f0b2273f7b6046c41a3aa7f2e2aacfb8ccd45bc122  62895777  zip_ok=True
- 2019q1.zip  49f6f0a00b6f1792d22893a3e261f7a329377048eb826079f1a16c7895b76d9d  95895180  zip_ok=True
- 2019q2.zip  765e42efd43687b4ac4ea59c83213857a52dca0a4bd5c490034bd5bea2b6fac3  90155728  zip_ok=True
- 2019q3.zip  4c57e36f04b54a2b9917537e12974cba68353f765affbf168bd506478780e13e  89829588  zip_ok=True
- 2019q4.zip  1f997c123b6d2fe892eeca0a5f12687e718ab5ae031f148b4a5d82f2689daec3  98753299  zip_ok=True
- 2020q1.zip  48ed9834c66d565c21130d087eae941b25770e2d7cc8351d17652612289fcd67  96584608  zip_ok=True
- 2020q2.zip  bd796ea10c88e21205da013f9c3f6a20b2b14d3de94f0075e9f529e0ef9a3543  82008710  zip_ok=True
- 2020q3.zip  6a402070d4b1f12db0d2256533b38705735684ecb58bc12ce53a61dd12abe26e  87582976  zip_ok=True
- 2020q4.zip  7cabec2c9b90511983623e6db3869b24c7a894466bd409f060a8da32790ae68e  89811373  zip_ok=True
- 2021q1.zip  6a776a9fa3d974476101efa9a4bfc531b7cdc2763abd45a6f70dd240a223843b  97900752  zip_ok=True
- 2021q2.zip  528b91432896eba74661c20481edb327809f3a0bf00e01d81821fe8e6078bc33  90864175  zip_ok=True
- 2021q3.zip  0fae2ceb149c0fe7e83b35b19157f910c864dee7e25d5ecf8b93dc42ee261b99  97252395  zip_ok=True
- 2021q4.zip  30d21449c0488e71dfb88ba8f85644a0e72194864e95111b3c1c120b3514c594  102642081  zip_ok=True
- 2022q1.zip  2ebeef77727a383f878bc28ff22498fd292a8dbab04216877b4b130ca00c1520  103669919  zip_ok=True
- 2022q2.zip  8939a4cafeabf7c94bde48be0e8c444f6332e4a9663698a89242eddd1036d27e  97089251  zip_ok=True
- 2022q3.zip  4bbfdc953177acf1a430a9f0a624dd8244bc81292d42872107d0446d06167c26  97589396  zip_ok=True
- 2022q4.zip  83bdc7a9545479eb64d118688c888ba8e7554ebc774f3ab8a8b0cc1decb87f28  111091525  zip_ok=True
- 2023q1.zip  479638c1be4088fa2ae42d2947468913eaff5ad0f093c412ba4340af8a8f791e  113974946  zip_ok=True
- 2023q2.zip  f028f1e8d24313e7619aa87c7c683a459452e7e81a8e5ba72d7ce39ddf62bda9  116914392  zip_ok=True
- 2023q3.zip  f075d721dde4393dabd49c1746578294ada4665ec29c85c2b3184f44f6f09187  117776944  zip_ok=True
- 2023q4.zip  ad23c5505e59b7305e16bfebb8bb839476689330b85a3e1ce216f809b18d1521  120272357  zip_ok=True
- 2024q1.zip  94c7894b00903f8ff7186c4250ba1c1daa1bde8b3a0b26fae68f72608b2ffc9b  124336804  zip_ok=True
- 2024q2.zip  698796e9f0946501d3dc74e7696a6c8c71cf32b66026e053722a784236b37761  119119954  zip_ok=True
- 2024q3.zip  b1823f2d65189f0d5ecbb1968d683c59df762129ae1b23bda0394093a69e0da9  118280418  zip_ok=True
- 2024q4.zip  9a2cbbaf39c94aa642b33d512f4def92b198d3bfd12d2267bcd27abe55b9902d  122932548  zip_ok=True
- 2025q1.zip  4386e7057cc216b6fc729d8f957998acb831d29be7b16f6d8ed0f6b1ab74eba5  127765057  zip_ok=True
- 2025q2.zip  81bf19bdb5606d5ee0164a8de8b7e96d5546170e42e59bcbf95b652ca83ac5d7  78973768  zip_ok=True
- 2025q3.zip  ebe25fa74c3655f4738ed9eb144af3953c3981c80f57d74829fb9ad68b114710  127842298  zip_ok=True
- 2025q4.zip  2b36ac3850c022cf19edd882e31c3c453c7666677b9fdd2e1f7748fdb5768c6e  65830170  zip_ok=True
- 2026q1.zip  d18c01c615da8f5cd46273b0dacaeee5b1163f4089171da0f84153a5f3c362f6  85259424  zip_ok=True
- 2026q2.zip  d7c815395cd420cfbe09b29dbae73a45406c5d0652d5ae1859ae11ea2e22df77  60419016  zip_ok=True
- 2026q3.zip  b6ebdc56b78526a6423f5b99f6e355afcb0510e7264324300ee5f0d3bd1bad21  65148008  zip_ok=True

### raw/ff

- F-F_Research_Data_5_Factors_2x3_daily_CSV.zip  7881223b51929ee0d6d23b8a28524aaec478a2c3d4eeca47224955cc845c5186  150292  zip_ok=True
- F-F_Momentum_Factor_daily_CSV.zip  ecf82b116e6ec56d2b79a2f27bd1d1598c217b5c6c25caa55b945bdd8a2c0b78  85269  zip_ok=True

## EODHD tickers fetched into raw/price/sp_exits

152 files: ADS ADT AET AGN AIV AMG AN ANDV ANTM ARG ATI AVP AYI BBBY BBT BCR BLL BXLT CA CAM CBS CCE CELG CFN CHK CMCSK CNX COG COL COTY CPGX CPRI CSRA CTL CVC CXO DAY DISCA DISCK DNB DNR DO DRE DTV DWDP EMC ESV ETFC EVHC FB FBHS FCPT FDO FI FL FLR FLS FRC FTI FTR GAS GGP GMCR GME GNW GPS HAR HBI HFC HOG HOT HP HRB HRS HSP INFO JEC JEF JOY JWN KORS KSS KSU LB LEG LLL LLTC LM LO LVLT M MAC MJN MNK MUR MWV MYL NBL NBR NE NFX NLOK NLSN NOV OI PBI PCL PCP PLL PRGO PVH PX QEP R RAI RHT RIG RRC RTN SBNY SCG SIG SLG SNI SPLS SRCL STJ SWN SWY SYMC TDC TE TEG TGNA THC TIF TMK TSS TWC TWTR TWX UA UAA UNM UTX VAR VNT WCG WLTW WU XEC XL
