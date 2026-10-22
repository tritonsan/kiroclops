"""Pixel-clock diagnostics must decode the image, not assume a frame number."""
import unittest
import numpy as np
from scripts.probe_svg_pixel_clock import decode


def marker(number):
    image=np.zeros((30,100,3),dtype=np.uint8)
    bits=[1,0,1,0]+[(number>>i)&1 for i in range(15,-1,-1)]
    crc=(number^(number>>8)^0xa7)&255
    bits += [(crc>>i)&1 for i in range(7,-1,-1)]
    for i,bit in enumerate(bits):
        for row in range(2):image[-6+row*3:-6+(row+1)*3 or None,16+i*3:19+i*3]=250 if bit^row else 5
    return image


class PixelClockProbeTests(unittest.TestCase):
    def test_counter_decodes_only_from_pixels(self):
        for n in (1,255,256,14973,65535):self.assertEqual(decode(marker(n)),n)

    def test_missing_corrupted_and_torn_tags_are_rejected(self):
        for image in (np.zeros((30,100,3),dtype=np.uint8),marker(17),marker(3)):
            if image.any():image[-5,30]=100
            with self.assertRaises(ValueError):decode(image)
        image=marker(15);image[-6:-3,-3:]=255-image[-6:-3,-3:]
        with self.assertRaises(ValueError):decode(image)

    def test_valid_looking_bit_flip_must_pass_checksum_as_well(self):
        image=marker(250)
        image[-6:,16+7*3:19+7*3]=255-image[-6:,16+7*3:19+7*3]
        with self.assertRaisesRegex(ValueError,'checksum'):decode(image)


if __name__=='__main__':unittest.main()
