C        Generated by TAPENADE     (INRIA, Ecuador team)
C  Tapenade 3.13 (r6666M) - 28 May 2018 09:28
C
C  Differentiation of bvbs_extrapolate in forward (tangent) mode:
C   variations   of useful results: rop
C   with respect to varying inputs: rop
C   RW status of diff variables: rop:in-out
C
C
C
C
C
C
C
C
C
C
C
C***********************************************************************
C     $Date: 2010-01-28 16:22:02 +0100 (Thu, 28 Jan 2010) 
C     $ $Revision: 56 $ 
C     $Author: IvanMary $
C*****a*****************************************************************
      SUBROUTINE BVBS_EXTRAPOLATE_D(idir, lrhs, eq_deb, param_int, 
     +                              ind_loop, nut_inf, rop, ropd)
      IMPLICIT NONE
C
C mise a zero si tableau drodm du RHS implicit
C
C
C      write(*,*)'idir=', idir,inc1,nijk(4),nijk(5),ndimdx
C      write(*,*)'nijk=', nijk
C      write(*,*)'loop=', ind_loop
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
C
      INTEGER*4 idir, lrhs, eq_deb, ind_loop(6), param_int(0:*)
C
      REAL*8 rop(param_int(41), param_int(36)), nut_inf
      REAL*8 ropd(param_int(41), param_int(36))
C Var local
      INTEGER*4 l, ijkm, im, jm, km, ldjr, i, j, k, inc1, ne, lij
      REAL*8 c1, vmax
C
C    adresse point courant pour tableau de la taille d'un domaine 
      INTEGER_E inddm, i_1, j_1, k_1
      INTRINSIC MAX
      REAL*8 max1
      REAL*8 max1d
      REAL*8 max2
      REAL*8 max2d
      REAL*8 max3
      REAL*8 max3d
C
      IF (lrhs .EQ. 1) THEN
        c1 = 0.
      ELSE
Cextrapolation ordre 0 si variable conservative ou primitive
        c1 = 1.
      END IF
C
      vmax = -1.e30
      IF (eq_deb .EQ. 6) vmax = nut_inf
C
      IF (idir .EQ. 1 .OR. idir .EQ. 2) THEN
C
        inc1 = 1
        IF (idir .EQ. 2) inc1 = ind_loop(1) - 1
C
        DO ne=eq_deb,param_int(36)
          DO k=ind_loop(5),ind_loop(6)
            DO j=ind_loop(3),ind_loop(4)
              ldjr = 1 + (inc1+param_int(0+3)-1) + (j+param_int(0+3)-1)*
     +          param_int(0) + (k+param_int(0+4)-1)*param_int(0)*
     +          param_int(0+1)
              DO i=ind_loop(1),ind_loop(2)
C
                l = 1 + (i+param_int(0+3)-1) + (j+param_int(0+3)-1)*
     +            param_int(0) + (k+param_int(0+4)-1)*param_int(0)*
     +            param_int(0+1)
                IF (vmax .LT. rop(ldjr, ne)) THEN
                  max1d = ropd(ldjr, ne)
                  max1 = rop(ldjr, ne)
                ELSE
                  max1 = vmax
                  max1d = 0.0
                END IF
C
                ropd(l, ne) = c1*max1d
              ENDDO
            ENDDO
          ENDDO
        ENDDO
      ELSE IF (idir .EQ. 3 .OR. idir .EQ. 4) THEN
C
C
        inc1 = 1
        IF (idir .EQ. 4) inc1 = ind_loop(3) - 1
C
        DO ne=eq_deb,param_int(36)
          DO k=ind_loop(5),ind_loop(6)
            DO j=ind_loop(3),ind_loop(4)
              DO i=ind_loop(1),ind_loop(2)
C
                ldjr = 1 + (i+param_int(0+3)-1) + (inc1+param_int(0+3)-1
     +            )*param_int(0) + (k+param_int(0+4)-1)*param_int(0)*
     +            param_int(0+1)
                l = 1 + (i+param_int(0+3)-1) + (j+param_int(0+3)-1)*
     +            param_int(0) + (k+param_int(0+4)-1)*param_int(0)*
     +            param_int(0+1)
                IF (vmax .LT. rop(ldjr, ne)) THEN
                  max2d = ropd(ldjr, ne)
                  max2 = rop(ldjr, ne)
                ELSE
                  max2 = vmax
                  max2d = 0.0
                END IF
C
                ropd(l, ne) = c1*max2d
              ENDDO
            ENDDO
          ENDDO
        ENDDO
      ELSE
C
C
        inc1 = 1
        IF (idir .EQ. 6) inc1 = ind_loop(5) - 1
C
        DO ne=eq_deb,param_int(36)
          DO k=ind_loop(5),ind_loop(6)
            DO j=ind_loop(3),ind_loop(4)
              DO i=ind_loop(1),ind_loop(2)
C
                ldjr = 1 + (i+param_int(0+3)-1) + (j+param_int(0+3)-1)*
     +            param_int(0) + (inc1+param_int(0+4)-1)*param_int(0)*
     +            param_int(0+1)
                l = 1 + (i+param_int(0+3)-1) + (j+param_int(0+3)-1)*
     +            param_int(0) + (k+param_int(0+4)-1)*param_int(0)*
     +            param_int(0+1)
                IF (vmax .LT. rop(ldjr, ne)) THEN
                  max3d = ropd(ldjr, ne)
                  max3 = rop(ldjr, ne)
                ELSE
                  max3 = vmax
                  max3d = 0.0
                END IF
C
                ropd(l, ne) = c1*max3d
              ENDDO
            ENDDO
          ENDDO
        ENDDO
      END IF
      END

