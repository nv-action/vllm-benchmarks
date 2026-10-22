import unittest

from validate_output import validate_decode, validate_metrics


class OutputValidationTests(unittest.TestCase):
    def test_metrics(self):
        validate_metrics("encoded 3 frames, Global PSNR: 60.558, SSIM Mean Y: 0.995")
        for text in (
            "PSNR Mean SSIM Mean",
            "encoded 2 frames, Global PSNR: 60, SSIM Mean Y: 0.99",
            "encoded 3 frames, Global PSNR: nan, SSIM Mean Y: 0.99",
            "encoded 3 frames, Global PSNR: 60, SSIM Mean Y: inf",
            "encoded 3 frames, Global PSNR: 60, SSIM Mean Y: 1.1",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_metrics(text)

    def test_decode(self):
        stream = dict(codec_name="hevc", width=64, height=64, nb_read_frames="3")
        validate_decode({"streams": [stream]}, 18432)
        for change in (
            {"codec_name": "h264"}, {"width": 128}, {"height": 128},
            {"nb_read_frames": "2"}, {"nb_read_frames": "N/A"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_decode({"streams": [{**stream, **change}]}, 18432)
        for size in (0, 12288, 24576):
            with self.subTest(size=size), self.assertRaises(ValueError):
                validate_decode({"streams": [stream]}, size)
        with self.assertRaises(ValueError):
            validate_decode({"streams": []}, 18432)


if __name__ == "__main__":
    unittest.main()
